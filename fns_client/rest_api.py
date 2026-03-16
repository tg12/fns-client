"""FastAPI application for NOTAM queries."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

try:
    import xmltodict
except ImportError:  # pragma: no cover
    xmltodict = None

from fns_client.database import NotamDb
from fns_client.messages import wrap_messages_as_xml

LOGGER = logging.getLogger(__name__)


def create_rest_app(notam_db: NotamDb, public_dir: Path | None = None) -> FastAPI:
    """Create the HTTP API around the NOTAM database."""

    app = FastAPI(title="FNS Client", version="2.0.0")

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
        limit: int = Query(default=5000, ge=1, le=20000),
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
        )
        return JSONResponse({"items": [_serialize_notam_row(row) for row in rows]})

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

    return app


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
