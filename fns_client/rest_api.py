# ARCHIVED — REST API still required as phantom-tide NOTAM data source (NOTAM_REST_URL)
# This file is kept for reference only. Do not edit.
# Migration date: 2026-03-19

# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""FastAPI application for NOTAM queries."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

try:
    import xmltodict
except ImportError:  # pragma: no cover
    xmltodict = None

from fns_client.database import NotamDb
from fns_client.messages import wrap_messages_as_xml
from fns_client.notam_intelligence import NotamIntelligenceEngine, SeverityLevel

LOGGER = logging.getLogger(__name__)

_AIRCRAFT_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "query": "",
    "payload": None,
}
_RATE_LIMIT_STATE: dict[str, tuple[int, int]] = {}


def create_rest_app(notam_db: NotamDb, public_dir: Path | None = None) -> FastAPI:
    """Create the HTTP API around the NOTAM database."""

    app = FastAPI(title="FNS Client", version="2.0.0")
    intelligence = NotamIntelligenceEngine()
    poller_url = os.getenv("OPENSKY_POLLER_URL", "http://opensky-poller:8081")
    aircraft_api_key = os.getenv("AIRCRAFT_PROXY_API_KEY", "").strip()
    aircraft_proxy_rpm = int(os.getenv("AIRCRAFT_PROXY_RPM", "0"))
    cache_ttl_seconds = float(os.getenv("AIRCRAFT_PROXY_CACHE_SECONDS", "2"))

    def _require_api_key(request: Request) -> None:
        if not aircraft_api_key:
            return
        header_value = request.headers.get("x-api-key", "")
        if header_value != aircraft_api_key:
            raise HTTPException(status_code=401, detail="Missing or invalid x-api-key")

    def _enforce_rate_limit(request: Request) -> None:
        client_host = request.client.host if request.client else "unknown"
        if _is_private_or_loopback_host(client_host):
            return
        if aircraft_proxy_rpm <= 0:
            return
        now_minute = int(time.time() // 60)
        minute, count = _RATE_LIMIT_STATE.get(client_host, (now_minute, 0))
        if minute != now_minute:
            minute = now_minute
            count = 0
        count += 1
        _RATE_LIMIT_STATE[client_host] = (minute, count)
        if count > aircraft_proxy_rpm:
            raise HTTPException(
                status_code=429, detail="Aircraft API rate limit exceeded"
            )

    @app.middleware("http")
    # type: ignore[override]
    async def enforce_database_validity(request: Request, call_next):
        # Guard data endpoints so clients do not read partial state during rebuilds.
        if (
            request.url.path.startswith(
                (
                    "/locationDesignator",
                    "/classification",
                    "/delta",
                    "/timerange",
                    "/allNotams",
                    "/notamTable",
                )
            )
            and not notam_db.is_valid()
        ):
            raise HTTPException(status_code=503, detail="NotamDb State Invalid")
        LOGGER.info(
            "Received request path=%s client=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        return await call_next(request)

    if public_dir is not None and public_dir.exists():
        app.mount("/public", StaticFiles(directory=str(public_dir)), name="public")

        @app.get("/")
        async def index() -> FileResponse:
            # Serve the local browser UI from the same process as the API.
            return FileResponse(public_dir / "index.html")

    @app.get("/health")
    async def health() -> JSONResponse:
        # Provide a lightweight health check for Docker and local diagnostics.
        snapshot = notam_db.get_status_snapshot()
        status_code = (
            200
            if snapshot["database_valid"] or snapshot["database_initializing"]
            else 503
        )
        return JSONResponse(snapshot, status_code=status_code)

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        # Expose summary data for the browser dashboard.
        return JSONResponse(notam_db.get_status_snapshot())

    @app.get("/api/notams")
    async def api_notams(
        location_designator: str | None = None,
        classification: str | None = None,
        updated_since: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_intelligence: bool = False,
        limit: int = Query(default=5000, ge=1, le=20000),
        include_archived: bool = False,
    ) -> JSONResponse:
        # Return structured NOTAM rows for the web UI and external API clients.
        rows = notam_db.list_notams(
            location_designator=location_designator,
            classification=classification,
            updated_since=(
                _parse_query_timestamp(updated_since) if updated_since else None
            ),
            valid_from=_parse_query_timestamp(valid_from) if valid_from else None,
            valid_to=_parse_query_timestamp(valid_to) if valid_to else None,
            limit=limit,
            include_archived=include_archived,
        )
        serialized_rows = [_serialize_notam_row(row) for row in rows]
        if include_intelligence:
            serialized_rows = [intelligence.enrich_row(row) for row in serialized_rows]
        return JSONResponse({"items": serialized_rows})

    @app.get("/api/notams/intelligence")
    async def api_notams_intelligence(
        location_designator: str | None = None,
        classification: str | None = None,
        updated_since: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_noise: bool = False,
        min_severity: str | None = "INFO",
        limit: int = Query(default=5000, ge=1, le=20000),
        include_archived: bool = False,
    ) -> JSONResponse:
        # Return NOTAMs ranked by operational importance with noise filtering.
        rows = notam_db.list_notams(
            location_designator=location_designator,
            classification=classification,
            updated_since=(
                _parse_query_timestamp(updated_since) if updated_since else None
            ),
            valid_from=_parse_query_timestamp(valid_from) if valid_from else None,
            valid_to=_parse_query_timestamp(valid_to) if valid_to else None,
            limit=limit,
            include_archived=include_archived,
        )
        serialized_rows = [_serialize_notam_row(row) for row in rows]
        try:
            minimum_level = SeverityLevel.from_query(min_severity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            intelligence.build_response(
                serialized_rows,
                include_noise=include_noise,
                min_severity=minimum_level,
            )
        )

    @app.get("/locationDesignator/{location_designator}")
    async def by_location_designator(
        location_designator: str, request: Request
    ) -> Response:
        messages = notam_db.get_by_location_designator(location_designator)
        return _render_messages(messages, request)

    @app.get("/classification/{classification}")
    async def by_classification(classification: str, request: Request) -> Response:
        messages = notam_db.get_by_classification(classification)
        return _render_messages(messages, request)

    @app.get("/delta/{delta_time}")
    async def by_delta(delta_time: str, request: Request) -> Response:
        parsed_delta = _parse_query_timestamp(delta_time)
        messages = notam_db.get_delta(parsed_delta)
        return _render_messages(messages, request)

    @app.get("/timerange/{from_datetime}/{to_datetime}")
    async def by_time_range(
        from_datetime: str, to_datetime: str, request: Request
    ) -> Response:
        messages = notam_db.get_by_time_range(
            _parse_query_timestamp(from_datetime),
            _parse_query_timestamp(to_datetime),
        )
        return _render_messages(messages, request)

    @app.get("/allNotams")
    async def all_notams(request: Request) -> Response:
        messages = notam_db.get_all_notams()
        return _render_messages(messages, request)

    @app.get("/notamTable/{location_designator}")
    async def notam_table(location_designator: str) -> JSONResponse:
        messages = notam_db.get_by_location_designator(location_designator)
        return JSONResponse(_messages_to_table(messages))

    @app.get("/api/aircraft")
    async def proxy_aircraft(request: Request) -> JSONResponse:
        # Proxy live aircraft states from the opensky-poller sidecar.
        # Returns empty data gracefully when the sidecar is unavailable.
        _require_api_key(request)
        _enforce_rate_limit(request)
        query_string = str(request.query_params)
        now = time.time()

        if (
            _AIRCRAFT_CACHE["payload"] is not None
            and _AIRCRAFT_CACHE["query"] == query_string
            and (now - _AIRCRAFT_CACHE["ts"]) <= cache_ttl_seconds
        ):
            payload = dict(_AIRCRAFT_CACHE["payload"])
            payload["cache"] = "hit"
            response = JSONResponse(payload)
            response.headers["Cache-Control"] = (
                f"private, max-age={int(cache_ttl_seconds)}"
            )
            return response

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{poller_url}/api/aircraft",
                    params=dict(request.query_params),
                )
                resp.raise_for_status()
                payload = resp.json()
                payload["cache"] = "miss"
                _AIRCRAFT_CACHE["ts"] = now
                _AIRCRAFT_CACHE["query"] = query_string
                _AIRCRAFT_CACHE["payload"] = payload
                response = JSONResponse(payload)
                response.headers["Cache-Control"] = (
                    f"private, max-age={int(cache_ttl_seconds)}"
                )
                return response
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("Aircraft proxy failed: %s", exc)
            return JSONResponse(
                {"count": 0, "states": [], "fetched_at": None, "error": str(exc)},
                status_code=200,
            )

    @app.post("/api/aircraft/control/bbox")
    async def proxy_aircraft_bbox_control(request: Request) -> JSONResponse:
        """Forward map bbox updates to the OpenSky poller for dynamic polling."""
        _require_api_key(request)
        _enforce_rate_limit(request)
        try:
            payload = await request.json()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{poller_url}/api/control/bbox",
                    json=payload,
                )
                resp.raise_for_status()
                return JSONResponse(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("Aircraft bbox control proxy failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @app.get("/api/aircraft/metrics")
    async def proxy_aircraft_metrics(request: Request) -> JSONResponse:
        """Proxy poller metrics for observability dashboards."""
        _require_api_key(request)
        _enforce_rate_limit(request)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{poller_url}/metrics")
                resp.raise_for_status()
                return JSONResponse(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("Aircraft metrics proxy failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @app.get("/api/aircraft/cache/batches")
    async def proxy_aircraft_cache_batches(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        success_only: bool | None = None,
    ) -> JSONResponse:
        """Proxy recent persisted OpenSky poll-batch metadata."""
        _require_api_key(request)
        _enforce_rate_limit(request)
        params: dict[str, Any] = {"limit": limit}
        if success_only is not None:
            params["success_only"] = success_only
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{poller_url}/api/cache/batches",
                    params=params,
                )
                if resp.status_code in {400, 404}:
                    return JSONResponse(resp.json(), status_code=resp.status_code)
                resp.raise_for_status()
                return JSONResponse(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("Aircraft cache batch proxy failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @app.get("/api/aircraft/cache/current")
    async def proxy_aircraft_cache_current(
        request: Request,
        limit: int = Query(default=500, ge=1, le=5000),
        visibility_status: str | None = None,
        icao24: str | None = None,
    ) -> JSONResponse:
        """Proxy current locally cached aircraft state with lifecycle metadata."""
        _require_api_key(request)
        _enforce_rate_limit(request)
        params: dict[str, Any] = {"limit": limit}
        if visibility_status:
            params["visibility_status"] = visibility_status
        if icao24:
            params["icao24"] = icao24
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{poller_url}/api/cache/current",
                    params=params,
                )
                if resp.status_code in {400, 404}:
                    return JSONResponse(resp.json(), status_code=resp.status_code)
                resp.raise_for_status()
                return JSONResponse(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("Aircraft cache current proxy failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @app.get("/api/aircraft/cache/history/{icao24}")
    async def proxy_aircraft_cache_history(
        icao24: str,
        request: Request,
        limit: int = Query(default=250, ge=1, le=5000),
    ) -> JSONResponse:
        """Proxy locally persisted aircraft history for one ICAO24."""
        _require_api_key(request)
        _enforce_rate_limit(request)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{poller_url}/api/cache/history/{icao24}",
                    params={"limit": limit},
                )
                if resp.status_code in {400, 404}:
                    return JSONResponse(resp.json(), status_code=resp.status_code)
                resp.raise_for_status()
                return JSONResponse(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("Aircraft cache history proxy failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @app.get("/api/aircraft/cache/raw/{batch_id}")
    async def proxy_aircraft_cache_raw(
        batch_id: int,
        request: Request,
    ) -> JSONResponse:
        """Proxy one archived raw OpenSky payload from local disk."""
        _require_api_key(request)
        _enforce_rate_limit(request)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(f"{poller_url}/api/cache/raw/{batch_id}")
                if resp.status_code in {400, 404}:
                    return JSONResponse(resp.json(), status_code=resp.status_code)
                resp.raise_for_status()
                return JSONResponse(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.warning("Aircraft cache raw proxy failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return app


def _is_private_or_loopback_host(host: str) -> bool:
    """Return True when the request host is local/private network traffic."""
    if host == "unknown":
        return True
    if host.startswith("localhost"):
        return True
    try:
        return ip_address(host).is_private or ip_address(host).is_loopback
    except ValueError:
        # Docker/K8s service names and internal hostnames are treated as local.
        return True


def _render_messages(messages: list[str], request: Request) -> Response:
    # Preserve the legacy XML endpoints while still supporting JSON clients.
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        payload = {
            "AixmBasicMessageCollection": [
                _xml_to_object(message) for message in messages
            ]
        }
        return JSONResponse(payload)
    return Response(
        content=wrap_messages_as_xml(messages), media_type="application/xml"
    )


def _messages_to_table(messages: list[str]) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []
    for message in messages:
        payload = _xml_to_object(message)
        flattened = _flatten_nested_mapping(payload)
        row = {
            key: value
            for key, value in flattened.items()
            if key.lower()
            in {
                "id",
                "location",
                "classification",
                "accountid",
                "text",
                "issued",
                "lastupdated",
            }
        }
        if row:
            table.append(row)
    return table


def _xml_to_object(xml_message: str) -> dict[str, Any]:
    # Parse XML lazily only for routes that still need the legacy payload shape.
    if xmltodict is None:
        return {"xml": xml_message}
    parsed = xmltodict.parse(xml_message)
    if isinstance(parsed, dict):
        return parsed
    return {"message": parsed}


def _flatten_nested_mapping(payload: Any, prefix: str = "") -> dict[str, Any]:
    # Flatten nested XML objects into a simple mapping for the legacy table route.
    if isinstance(payload, dict):
        flattened: dict[str, Any] = {}
        for key, value in payload.items():
            nested_prefix = f"{prefix}{key.split(':')[-1]}"
            flattened.update(_flatten_nested_mapping(value, f"{nested_prefix}."))
        return flattened
    if isinstance(payload, list):
        flattened: dict[str, Any] = {}
        for item in payload:
            flattened.update(_flatten_nested_mapping(item, prefix))
        return flattened
    return {prefix[:-1]: payload}


def _parse_query_timestamp(value: str) -> datetime:
    # Support the legacy timestamp formats plus ISO-8601 input.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


def _serialize_notam_row(row: dict[str, Any]) -> dict[str, Any]:
    """Serialize one database row into JSON-friendly values."""

    # Convert datetimes explicitly so the API payload stays stable and browser-friendly.
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result
