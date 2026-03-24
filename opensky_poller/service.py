# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""OpenSky Network poller.

Polls the OpenSky REST API on a fixed interval, caches the latest aircraft
state vectors in memory, and exposes them via a lightweight FastAPI endpoint.
The fns-client sidecar proxies /api/aircraft to this service.

Rate limits (as of 2026):
  Anonymous  -- 10 API credits / 10 s, roughly one global call per 10 s.
  Registered -- 4000 credits / day; queries with bbox consume fewer credits.

References:
  https://openskynetwork.github.io/opensky-api/rest.html
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from persistence import LocalObservationStore

# ---------------------------------------------------------------------------
# Static configuration
# ---------------------------------------------------------------------------

OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = os.getenv(
    "OPENSKY_TOKEN_URL",
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
)

POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))
MIN_POLL_INTERVAL_SECONDS: int = int(os.getenv("MIN_POLL_INTERVAL_SECONDS", "60"))
MAX_BACKOFF_SECONDS: int = int(os.getenv("MAX_BACKOFF_SECONDS", "900"))
OPENSKY_USERNAME: str = os.getenv("OPENSKY_USERNAME", "").strip()
OPENSKY_PASSWORD: str = os.getenv("OPENSKY_PASSWORD", "").strip()
OPENSKY_CLIENT_ID: str = os.getenv("OPENSKY_CLIENT_ID", "").strip()
OPENSKY_CLIENT_SECRET: str = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
TOKEN_REFRESH_MARGIN_SECONDS: int = int(os.getenv("TOKEN_REFRESH_MARGIN_SECONDS", "30"))
LOW_CREDIT_WARNING_THRESHOLD: int = int(
    os.getenv("LOW_CREDIT_WARNING_THRESHOLD", "250")
)
PROJECTED_CREDIT_WARNING_RATIO: float = float(
    os.getenv("PROJECTED_CREDIT_WARNING_RATIO", "0.85")
)
HISTORY_MINUTES: int = int(os.getenv("HISTORY_MINUTES", "60"))
CONTROL_BBOX_TTL_SECONDS: int = int(os.getenv("CONTROL_BBOX_TTL_SECONDS", "120"))
STALE_AFTER_SECONDS: int = int(os.getenv("STALE_AFTER_SECONDS", "30"))
OPENSKY_DATA_DIR = Path(os.getenv("OPENSKY_DATA_DIR", "/app/data/opensky"))

# Optional bounding box applied at fetch time to reduce response size.
# Defaults to global; set via environment to a relevant region.
BBOX_LAT_MIN: float = float(os.getenv("BBOX_LAT_MIN", "-90"))
BBOX_LAT_MAX: float = float(os.getenv("BBOX_LAT_MAX", "90"))
BBOX_LON_MIN: float = float(os.getenv("BBOX_LON_MIN", "-180"))
BBOX_LON_MAX: float = float(os.getenv("BBOX_LON_MAX", "180"))

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AircraftState:
    """Parsed OpenSky state vector for a single aircraft.

    Field positions follow the OpenSky API documentation v1.
    https://openskynetwork.github.io/opensky-api/rest.html#state-vectors
    """

    icao24: str
    callsign: str
    origin_country: str
    time_position: int | None
    last_contact: int
    longitude: float
    latitude: float
    baro_altitude: float | None
    on_ground: bool
    velocity: float | None
    true_track: float | None
    vertical_rate: float | None
    geo_altitude: float | None
    squawk: str | None
    position_source: int
    category: int | None


# ---------------------------------------------------------------------------
# Shared in-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {
    "states": [],
    "fetched_at": None,
    "usage_window_started_at": None,
    "next_poll_due_at": None,
    "next_poll_delay_seconds": None,
    "consecutive_failures": 0,
    "fetch_count": 0,
    "successful_fetch_count": 0,
    "last_error": None,
    "total_errors": 0,
    "last_poll_latency_ms": None,
    "avg_poll_latency_ms": None,
    "last_http_status": None,
    "last_rate_limit_remaining": None,
    "last_retry_after_seconds": None,
    "auth_mode": "anonymous",
    "last_estimated_credit_cost": 0,
    "estimated_credit_usage": 0,
    "active_bbox": None,
}

_trail_points: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
_requested_bbox: dict[str, float] | None = None
_requested_bbox_at: int | None = None
_oauth_access_token: str | None = None
_oauth_token_expires_at: int | None = None
_store = LocalObservationStore(OPENSKY_DATA_DIR)

_lock = asyncio.Lock()
_auth_lock = asyncio.Lock()


def _parse_state_vector(sv: list[Any]) -> AircraftState | None:
    """Parse a raw OpenSky state vector list.

    Returns None for vectors with no position data or on parse failure.
    Field layout is positional; see OpenSky API docs.
    """
    try:
        lat = sv[6]
        lon = sv[5]
        if lat is None or lon is None:
            return None
        return AircraftState(
            icao24=sv[0] or "",
            callsign=(sv[1] or "").strip(),
            origin_country=sv[2] or "",
            time_position=sv[3],
            last_contact=int(sv[4]) if sv[4] is not None else 0,
            longitude=float(lon),
            latitude=float(lat),
            baro_altitude=float(sv[7]) if sv[7] is not None else None,
            on_ground=bool(sv[8]),
            velocity=float(sv[9]) if sv[9] is not None else None,
            true_track=float(sv[10]) if sv[10] is not None else None,
            vertical_rate=float(sv[11]) if sv[11] is not None else None,
            geo_altitude=float(sv[13]) if sv[13] is not None else None,
            squawk=sv[14],
            position_source=int(sv[16]) if len(sv) > 16 and sv[16] is not None else 0,
            category=int(sv[17]) if len(sv) > 17 and sv[17] is not None else None,
        )
    except (IndexError, TypeError, ValueError) as exc:
        LOGGER.debug("Skipping malformed state vector: %s", exc)
        return None


def _current_auth_mode() -> str:
    """Return the configured OpenSky authentication mode."""
    if OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET:
        return "oauth2"
    if OPENSKY_USERNAME and OPENSKY_PASSWORD:
        return "basic"
    return "anonymous"


def _bbox_area_square_degrees(params: dict[str, Any]) -> float | None:
    """Return bounding-box area in square degrees when a bbox is active."""
    required = ("lamin", "lamax", "lomin", "lomax")
    if not all(key in params for key in required):
        return None
    try:
        lat_span = max(0.0, float(params["lamax"]) - float(params["lamin"]))
        lon_span = max(0.0, float(params["lomax"]) - float(params["lomin"]))
    except (TypeError, ValueError):
        return None
    return round(lat_span * lon_span, 2)


def _estimate_credit_cost(params: dict[str, Any]) -> int:
    """Estimate OpenSky credit cost for a states/all request from bbox area."""
    area = _bbox_area_square_degrees(params)
    if area is None:
        return 4
    if area <= 25:
        return 1
    if area <= 100:
        return 2
    if area <= 400:
        return 3
    return 4


def _credit_budget_for_auth_mode(auth_mode: str) -> int:
    """Return the assumed daily credit budget for the current auth mode."""
    if auth_mode == "anonymous":
        return 400
    return 4000


async def _log_usage_status(*, status_code: int, aircraft_count: int) -> None:
    """Emit operator-focused logs describing current OpenSky usage headroom."""
    async with _lock:
        auth_mode = str(_cache["auth_mode"])
        remaining = _cache["last_rate_limit_remaining"]
        retry_after = _cache["last_retry_after_seconds"]
        estimated_credit_cost = int(_cache["last_estimated_credit_cost"])
        estimated_credit_usage = int(_cache["estimated_credit_usage"])
        successful_fetch_count = int(_cache["successful_fetch_count"])
        usage_window_started_at = _cache["usage_window_started_at"]
        active_bbox = _cache["active_bbox"]

    area = _bbox_area_square_degrees(active_bbox or {})
    budget = _credit_budget_for_auth_mode(auth_mode)
    projected_daily_credits = _projected_daily_credit_usage(
        estimated_credit_usage=estimated_credit_usage,
        usage_window_started_at=usage_window_started_at,
    )

    LOGGER.info(
        "OpenSky usage: auth_mode=%s status=%s cost_est=%s credits remaining=%s projected_daily_credits=%s budget=%s bbox_area_sqdeg=%s aircraft=%s success_count=%s",
        auth_mode,
        status_code,
        estimated_credit_cost,
        remaining if remaining is not None else "n/a",
        projected_daily_credits,
        budget,
        area if area is not None else "global",
        aircraft_count,
        successful_fetch_count,
    )

    if remaining is not None and remaining <= LOW_CREDIT_WARNING_THRESHOLD:
        LOGGER.warning(
            "OpenSky remaining credits are low: remaining=%s threshold=%s auth_mode=%s",
            remaining,
            LOW_CREDIT_WARNING_THRESHOLD,
            auth_mode,
        )
    if projected_daily_credits >= int(budget * PROJECTED_CREDIT_WARNING_RATIO):
        LOGGER.warning(
            "OpenSky projected daily credit usage is high: projected=%s budget=%s ratio=%.2f auth_mode=%s",
            projected_daily_credits,
            budget,
            PROJECTED_CREDIT_WARNING_RATIO,
            auth_mode,
        )
    if retry_after:
        LOGGER.warning(
            "OpenSky requested retry backoff: retry_after_seconds=%s auth_mode=%s",
            retry_after,
            auth_mode,
        )


async def _persist_poll_result(
    *,
    polled_at: int,
    status_code: int | None,
    success: bool,
    bbox: dict[str, Any],
    latency_ms: float | None,
    raw_payload: dict[str, Any] | None,
    error_text: str | None,
    aircraft_states: list[dict[str, Any]],
) -> None:
    """Persist one upstream poll into the local observation store."""
    async with _lock:
        auth_mode = str(_cache["auth_mode"])
        rate_limit_remaining = _cache["last_rate_limit_remaining"]
        retry_after_seconds = _cache["last_retry_after_seconds"]
        estimated_credit_cost = int(_cache["last_estimated_credit_cost"])
    try:
        stats = _store.record_poll(
            polled_at=polled_at,
            status_code=status_code,
            success=success,
            auth_mode=auth_mode,
            bbox=bbox or None,
            bbox_area_sqdeg=_bbox_area_square_degrees(bbox),
            latency_ms=latency_ms,
            rate_limit_remaining=rate_limit_remaining,
            retry_after_seconds=retry_after_seconds,
            estimated_credit_cost=estimated_credit_cost,
            aircraft_states=aircraft_states,
            raw_payload=raw_payload,
            error_text=error_text,
        )
        LOGGER.info(
            "OpenSky local cache persisted: batches=%s current=%s history=%s",
            stats["stored_batch_count"],
            stats["stored_current_count"],
            stats["stored_history_count"],
        )
    except sqlite3.Error as exc:
        LOGGER.error("OpenSky persistence failed: %s", exc, exc_info=True)


def _projected_daily_credit_usage(
    *,
    estimated_credit_usage: int,
    usage_window_started_at: int | None,
) -> int:
    """Project daily credit usage from current runtime behavior."""
    if usage_window_started_at is None:
        return 0
    runtime_seconds = max(1, int(time.time()) - int(usage_window_started_at) + 1)
    return int(round(estimated_credit_usage * (86400 / runtime_seconds)))


async def _poll_opensky(client: httpx.AsyncClient) -> tuple[bool, int | None]:
    """Fetch aircraft states from OpenSky and replace the cache."""
    params: dict[str, Any] = {}
    static_bbox = {
        "lamin": BBOX_LAT_MIN,
        "lomin": BBOX_LON_MIN,
        "lamax": BBOX_LAT_MAX,
        "lomax": BBOX_LON_MAX,
    }
    now_ts = int(time.time())
    async with _lock:
        if _requested_bbox is None:
            dynamic_bbox = None
        elif CONTROL_BBOX_TTL_SECONDS <= 0:
            # Keep the last UI-selected bbox indefinitely when TTL is disabled.
            dynamic_bbox = dict(_requested_bbox)
        elif (
            _requested_bbox_at
            and now_ts - _requested_bbox_at <= CONTROL_BBOX_TTL_SECONDS
        ):
            dynamic_bbox = dict(_requested_bbox)
        else:
            dynamic_bbox = None

    # Prefer recent UI bbox for credit efficiency, fallback to static config bbox.
    if dynamic_bbox is not None:
        params = dynamic_bbox
    elif not (
        BBOX_LAT_MIN == -90
        and BBOX_LAT_MAX == 90
        and BBOX_LON_MIN == -180
        and BBOX_LON_MAX == 180
    ):
        params = static_bbox
        params["extended"] = 1

    auth: tuple[str, str] | None = None
    headers: dict[str, str] = {}
    bearer_token = await _get_oauth_token(client)
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if not headers and OPENSKY_USERNAME and OPENSKY_PASSWORD:
        # Legacy fallback while migrating to OAuth2 client credentials.
        auth = (OPENSKY_USERNAME, OPENSKY_PASSWORD)

    try:
        poll_started = time.perf_counter()
        resp = await client.get(
            OPENSKY_URL,
            params=params,
            headers=headers or None,
            auth=auth,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        states_raw: list[list[Any]] = data.get("states") or []
        parsed = [s for sv in states_raw if (s := _parse_state_vector(sv)) is not None]
        latency_ms = round((time.perf_counter() - poll_started) * 1000.0, 2)
        cutoff_ts = now_ts - (HISTORY_MINUTES * 60)
        async with _lock:
            _cache["states"] = [asdict(s) for s in parsed]
            _cache["fetched_at"] = now_ts
            if _cache["usage_window_started_at"] is None:
                _cache["usage_window_started_at"] = now_ts
            _cache["fetch_count"] += 1
            _cache["successful_fetch_count"] += 1
            _cache["last_error"] = None
            _cache["last_http_status"] = resp.status_code
            _cache["auth_mode"] = _current_auth_mode()
            _cache["last_estimated_credit_cost"] = _estimate_credit_cost(params)
            _cache["estimated_credit_usage"] += _cache["last_estimated_credit_cost"]
            _cache["last_rate_limit_remaining"] = _parse_positive_int_header(
                resp.headers.get("x-rate-limit-remaining")
            )
            _cache["last_retry_after_seconds"] = _parse_positive_int_header(
                resp.headers.get("x-rate-limit-retry-after-seconds")
            )
            _cache["active_bbox"] = params if params else None
            _cache["last_poll_latency_ms"] = latency_ms
            if _cache["avg_poll_latency_ms"] is None:
                _cache["avg_poll_latency_ms"] = latency_ms
            else:
                _cache["avg_poll_latency_ms"] = round(
                    (_cache["avg_poll_latency_ms"] * 0.85) + (latency_ms * 0.15),
                    2,
                )

            for aircraft in parsed:
                trail = _trail_points[aircraft.icao24]
                trail.append(
                    {
                        "t": now_ts,
                        "lat": aircraft.latitude,
                        "lon": aircraft.longitude,
                        "alt": aircraft.baro_altitude,
                        "ground": aircraft.on_ground,
                    }
                )
                while trail and trail[0]["t"] < cutoff_ts:
                    trail.popleft()

            inactive = [
                key
                for key, points in _trail_points.items()
                if not points or points[-1]["t"] < cutoff_ts
            ]
            for key in inactive:
                _trail_points.pop(key, None)

        LOGGER.info(
            "OpenSky poll #%d: %d aircraft with position latency=%sms",
            _cache["fetch_count"],
            len(parsed),
            latency_ms,
        )
        await _persist_poll_result(
            polled_at=now_ts,
            status_code=resp.status_code,
            success=True,
            bbox=params,
            latency_ms=latency_ms,
            raw_payload=data,
            error_text=None,
            aircraft_states=_cache["states"],
        )
        await _log_usage_status(
            status_code=resp.status_code, aircraft_count=len(parsed)
        )
        return True, resp.status_code
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            await _clear_oauth_token_cache()
        body = exc.response.text[:200]
        LOGGER.error("OpenSky HTTP error: %s %s", status, body)
        retry_after = _parse_positive_int_header(
            exc.response.headers.get("x-rate-limit-retry-after-seconds")
        )
        if retry_after is None:
            retry_after = _parse_positive_int_header(
                exc.response.headers.get("retry-after")
            )
        async with _lock:
            _cache["last_error"] = f"HTTP {status}"
            _cache["last_http_status"] = status
            _cache["auth_mode"] = _current_auth_mode()
            _cache["last_estimated_credit_cost"] = _estimate_credit_cost(params)
            _cache["last_rate_limit_remaining"] = _parse_positive_int_header(
                exc.response.headers.get("x-rate-limit-remaining")
            )
            _cache["last_retry_after_seconds"] = retry_after
            _cache["total_errors"] += 1
        await _persist_poll_result(
            polled_at=now_ts,
            status_code=status,
            success=False,
            bbox=params,
            latency_ms=None,
            raw_payload=None,
            error_text=f"HTTP {status}",
            aircraft_states=[],
        )
        await _log_usage_status(status_code=status, aircraft_count=0)
        return False, status
    except httpx.RequestError as exc:
        LOGGER.error("OpenSky request error: %s", exc)
        async with _lock:
            _cache["last_error"] = str(exc)
            _cache["last_http_status"] = None
            _cache["auth_mode"] = _current_auth_mode()
            _cache["last_retry_after_seconds"] = None
            _cache["total_errors"] += 1
        await _persist_poll_result(
            polled_at=now_ts,
            status_code=None,
            success=False,
            bbox=params,
            latency_ms=None,
            raw_payload=None,
            error_text=str(exc),
            aircraft_states=[],
        )
        await _log_usage_status(status_code=0, aircraft_count=0)
        return False, None


async def _background_poller() -> None:
    """Poll OpenSky repeatedly until cancelled."""
    base_interval_seconds = max(POLL_INTERVAL, MIN_POLL_INTERVAL_SECONDS, 60)
    current_delay_seconds = base_interval_seconds

    async with httpx.AsyncClient() as client:
        while True:
            success, status_code = await _poll_opensky(client)
            async with _lock:
                retry_after_seconds = _cache["last_retry_after_seconds"]

            if success:
                current_delay_seconds = base_interval_seconds
                async with _lock:
                    _cache["consecutive_failures"] = 0
            else:
                async with _lock:
                    _cache["consecutive_failures"] += 1
                if status_code == 429:
                    current_delay_seconds = min(
                        max(current_delay_seconds * 2, retry_after_seconds or 0),
                        MAX_BACKOFF_SECONDS,
                    )
                else:
                    current_delay_seconds = min(
                        max(base_interval_seconds, int(current_delay_seconds * 1.5)),
                        MAX_BACKOFF_SECONDS,
                    )

            async with _lock:
                _cache["next_poll_delay_seconds"] = current_delay_seconds
                _cache["next_poll_due_at"] = int(time.time()) + current_delay_seconds

            await asyncio.sleep(current_delay_seconds)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """Configure structured logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background poller on startup and cancel it on shutdown."""
    _store.initialize()
    restored = _store.restore_snapshot(HISTORY_MINUTES)
    usage_summary = _store.usage_summary()
    async with _lock:
        _cache["states"] = restored["states"]
        _cache["fetched_at"] = restored["fetched_at"]
        _cache["active_bbox"] = restored["active_bbox"]
        _cache["estimated_credit_usage"] = usage_summary["estimated_credit_usage"]
        _cache["usage_window_started_at"] = usage_summary["usage_window_started_at"]
        _cache["fetch_count"] = usage_summary["fetch_count"]
        _cache["successful_fetch_count"] = usage_summary["successful_fetch_count"]
    _trail_points.clear()
    for icao24, points in restored["trails"].items():
        _trail_points[icao24].extend(points)

    task = asyncio.create_task(_background_poller())
    LOGGER.info(
        "OpenSky poller started: base_interval=%ds max_backoff=%ds bbox=[%.1f,%.1f,%.1f,%.1f]",
        POLL_INTERVAL,
        MAX_BACKOFF_SECONDS,
        BBOX_LAT_MIN,
        BBOX_LAT_MAX,
        BBOX_LON_MIN,
        BBOX_LON_MAX,
    )
    LOGGER.info(
        "OpenSky local store initialized: data_dir=%s restored_states=%s restored_trails=%s",
        OPENSKY_DATA_DIR,
        len(restored["states"]),
        len(restored["trails"]),
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    LOGGER.info("OpenSky poller stopped.")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    configure_logging()
    application = FastAPI(
        title="OpenSky Poller",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Allow same-origin and cross-origin browser requests (sidecar model).
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    valid_visibility_statuses = {"seen", "stale_unseen", "out_of_scope"}

    async def _health_snapshot() -> dict[str, Any]:
        """Return poller health and cache metadata."""
        async with _lock:
            stale_seconds = (
                None
                if _cache["fetched_at"] is None
                else int(time.time()) - int(_cache["fetched_at"])
            )
            return {
                "live": True,
                "ready": _cache["fetched_at"] is not None,
                "fetched_at": _cache["fetched_at"],
                "next_poll_due_at": _cache["next_poll_due_at"],
                "next_poll_delay_seconds": _cache["next_poll_delay_seconds"],
                "consecutive_failures": _cache["consecutive_failures"],
                "auth_mode": _cache["auth_mode"],
                "last_estimated_credit_cost": _cache["last_estimated_credit_cost"],
                "estimated_credit_usage": _cache["estimated_credit_usage"],
                "projected_daily_credits": _projected_daily_credit_usage(
                    estimated_credit_usage=int(_cache["estimated_credit_usage"]),
                    usage_window_started_at=_cache["usage_window_started_at"],
                ),
                "last_rate_limit_remaining": _cache["last_rate_limit_remaining"],
                "last_retry_after_seconds": _cache["last_retry_after_seconds"],
                "aircraft_count": len(_cache["states"]),
                "fetch_count": _cache["fetch_count"],
                "last_error": _cache["last_error"],
                "total_errors": _cache["total_errors"],
                "last_poll_latency_ms": _cache["last_poll_latency_ms"],
                "avg_poll_latency_ms": _cache["avg_poll_latency_ms"],
                "last_http_status": _cache["last_http_status"],
                "stale_seconds": stale_seconds,
                "stale_alarm": bool(
                    stale_seconds is not None and stale_seconds > STALE_AFTER_SECONDS
                ),
                **_store.stats(),
            }

    @application.get("/live")
    async def live() -> JSONResponse:
        """Return container liveness regardless of upstream availability."""
        return JSONResponse({"live": True}, status_code=200)

    @application.get("/ready")
    async def ready() -> JSONResponse:
        """Return readiness based on whether a usable OpenSky snapshot exists."""
        snap = await _health_snapshot()
        return JSONResponse(snap, status_code=200 if snap["ready"] else 503)

    @application.get("/health")
    async def health() -> JSONResponse:
        """Return liveness-oriented health details for Docker/local checks."""
        return JSONResponse(await _health_snapshot(), status_code=200)

    @application.post("/api/control/bbox")
    async def control_bbox(payload: dict[str, float]) -> JSONResponse:
        """Accept a UI-provided map bbox to constrain OpenSky polling."""
        try:
            lamin = float(payload["lamin"])
            lomin = float(payload["lomin"])
            lamax = float(payload["lamax"])
            lomax = float(payload["lomax"])
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        if not (
            -90 <= lamin <= 90
            and -90 <= lamax <= 90
            and -180 <= lomin <= 180
            and -180 <= lomax <= 180
        ):
            return JSONResponse(
                {"ok": False, "error": "bbox out of range"}, status_code=400
            )
        if lamin >= lamax or lomin >= lomax:
            return JSONResponse(
                {"ok": False, "error": "invalid bbox ordering"}, status_code=400
            )

        async with _lock:
            global _requested_bbox, _requested_bbox_at
            _requested_bbox = {
                "lamin": round(lamin, 4),
                "lomin": round(lomin, 4),
                "lamax": round(lamax, 4),
                "lomax": round(lomax, 4),
            }
            _requested_bbox_at = int(time.time())
        return JSONResponse(
            {"ok": True, "bbox": _requested_bbox, "requested_at": _requested_bbox_at}
        )

    @application.get("/metrics")
    async def metrics() -> JSONResponse:
        """Return operational metrics for dashboards and alerting."""
        async with _lock:
            fetched_at = _cache["fetched_at"]
            stale_seconds = (
                None if fetched_at is None else int(time.time()) - int(fetched_at)
            )
            credits_per_day = (
                _cache["fetch_count"]
                * (86400 / max(1, int(time.time() - fetched_at + 1)))
                if fetched_at
                else 0
            )
            return JSONResponse(
                {
                    "poll_interval_seconds": POLL_INTERVAL,
                    "min_poll_interval_seconds": MIN_POLL_INTERVAL_SECONDS,
                    "max_backoff_seconds": MAX_BACKOFF_SECONDS,
                    "next_poll_due_at": _cache["next_poll_due_at"],
                    "next_poll_delay_seconds": _cache["next_poll_delay_seconds"],
                    "consecutive_failures": _cache["consecutive_failures"],
                    "auth_mode": _cache["auth_mode"],
                    "last_estimated_credit_cost": _cache["last_estimated_credit_cost"],
                    "estimated_credit_usage": _cache["estimated_credit_usage"],
                    "projected_daily_credits": _projected_daily_credit_usage(
                        estimated_credit_usage=int(_cache["estimated_credit_usage"]),
                        usage_window_started_at=_cache["usage_window_started_at"],
                    ),
                    "last_rate_limit_remaining": _cache["last_rate_limit_remaining"],
                    "last_retry_after_seconds": _cache["last_retry_after_seconds"],
                    "fetch_count": _cache["fetch_count"],
                    "total_errors": _cache["total_errors"],
                    "last_http_status": _cache["last_http_status"],
                    "last_poll_latency_ms": _cache["last_poll_latency_ms"],
                    "avg_poll_latency_ms": _cache["avg_poll_latency_ms"],
                    "aircraft_count": len(_cache["states"]),
                    "stale_seconds": stale_seconds,
                    "stale_alarm": bool(
                        stale_seconds is not None
                        and stale_seconds > STALE_AFTER_SECONDS
                    ),
                    "active_bbox": _cache["active_bbox"],
                    "requested_bbox": _requested_bbox,
                    "requested_bbox_age_seconds": (
                        None
                        if _requested_bbox_at is None
                        else int(time.time()) - _requested_bbox_at
                    ),
                    "estimated_daily_calls": round(credits_per_day, 2),
                    **_store.stats(),
                }
            )

    @application.get("/api/cache/batches")
    async def cache_batches(
        limit: int = Query(default=50, ge=1, le=500),
        success_only: bool | None = Query(
            default=None, description="Filter to only successful or failed polls"
        ),
    ) -> JSONResponse:
        """Return recent persisted poll-batch metadata from local disk."""
        return JSONResponse(
            {
                "items": _store.list_poll_batches(
                    limit=limit,
                    success_only=success_only,
                )
            }
        )

    @application.get("/api/cache/current")
    async def cache_current(
        limit: int = Query(default=500, ge=1, le=5000),
        visibility_status: str | None = Query(
            default=None,
            description="seen, stale_unseen, or out_of_scope",
        ),
        icao24: str | None = Query(default=None, description="Exact ICAO24 hex id"),
    ) -> JSONResponse:
        """Return current cached aircraft state with lifecycle metadata."""
        if visibility_status and visibility_status not in valid_visibility_statuses:
            return JSONResponse(
                {
                    "error": (
                        "visibility_status must be one of "
                        + ", ".join(sorted(valid_visibility_statuses))
                    )
                },
                status_code=400,
            )

        return JSONResponse(
            {
                "items": _store.list_current_aircraft(
                    limit=limit,
                    visibility_status=visibility_status,
                    icao24=icao24,
                )
            }
        )

    @application.get("/api/cache/history/{icao24}")
    async def cache_history(
        icao24: str,
        limit: int = Query(default=250, ge=1, le=5000),
    ) -> JSONResponse:
        """Return persisted change history for one aircraft."""
        return JSONResponse(
            {
                "icao24": icao24.strip().lower(),
                "items": _store.list_aircraft_history(
                    icao24=icao24,
                    limit=limit,
                ),
            }
        )

    @application.get("/api/cache/raw/{batch_id}")
    async def cache_raw_payload(batch_id: int) -> JSONResponse:
        """Return one archived raw OpenSky response payload from local disk."""
        try:
            payload = _store.get_batch_payload(batch_id)
        except FileNotFoundError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        if payload is None:
            return JSONResponse(
                {"error": f"no archived payload available for batch {batch_id}"},
                status_code=404,
            )
        return JSONResponse(payload)

    @application.get("/api/aircraft")
    async def get_aircraft(
        lamin: float | None = Query(default=None, description="Min latitude"),
        lamax: float | None = Query(default=None, description="Max latitude"),
        lomin: float | None = Query(default=None, description="Min longitude"),
        lomax: float | None = Query(default=None, description="Max longitude"),
        include_trails: int = Query(default=1, ge=0, le=1),
        history_minutes: int = Query(default=30, ge=1, le=60),
    ) -> JSONResponse:
        """Return cached aircraft states, optionally filtered by bounding box."""
        async with _lock:
            states: list[dict[str, Any]] = list(_cache["states"])
            fetched_at = _cache["fetched_at"]
            last_error = _cache["last_error"]
            active_bbox = _cache["active_bbox"]

        # Client-side bbox filter: let the browser scope to the visible map area.
        if all(v is not None for v in [lamin, lamax, lomin, lomax]):
            states = [
                s
                for s in states
                if s["latitude"] is not None
                and s["longitude"] is not None
                and lamin <= s["latitude"] <= lamax  # type: ignore[operator]
                and lomin <= s["longitude"] <= lomax  # type: ignore[operator]
            ]

        trails: dict[str, list[dict[str, Any]]] = {}
        if include_trails == 1:
            cutoff = int(time.time()) - (history_minutes * 60)
            async with _lock:
                for aircraft in states:
                    key = aircraft["icao24"]
                    points = _trail_points.get(key)
                    if not points:
                        continue
                    recent = [p for p in points if p["t"] >= cutoff]
                    if recent:
                        trails[key] = recent

        return JSONResponse(
            {
                "fetched_at": fetched_at,
                "count": len(states),
                "states": states,
                "trails": trails,
                "active_bbox": active_bbox,
                "last_error": last_error,
                "history_minutes": history_minutes,
            }
        )

    return application


app = create_app()


def _parse_positive_int_header(value: str | None) -> int | None:
    """Parse a positive integer header value, returning None on invalid input."""
    if value is None:
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


async def _get_oauth_token(client: httpx.AsyncClient) -> str | None:
    """Return a valid cached OAuth token, refreshing when required."""
    global _oauth_access_token, _oauth_token_expires_at

    if not OPENSKY_CLIENT_ID or not OPENSKY_CLIENT_SECRET:
        return None

    now_ts = int(time.time())
    async with _auth_lock:
        if (
            _oauth_access_token
            and _oauth_token_expires_at
            and now_ts < _oauth_token_expires_at
        ):
            return _oauth_access_token

        try:
            resp = await client.post(
                OPENSKY_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": OPENSKY_CLIENT_ID,
                    "client_secret": OPENSKY_CLIENT_SECRET,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            LOGGER.error("OpenSky token refresh failed: %s", exc)
            return None

        token = str(payload.get("access_token") or "").strip()
        expires_in = payload.get("expires_in")
        if not token:
            LOGGER.error("OpenSky token refresh failed: missing access_token")
            return None
        if not isinstance(expires_in, int) or expires_in <= 0:
            expires_in = 1800

        _oauth_access_token = token
        _oauth_token_expires_at = now_ts + max(
            1,
            expires_in - TOKEN_REFRESH_MARGIN_SECONDS,
        )
        return _oauth_access_token


async def _clear_oauth_token_cache() -> None:
    """Invalidate cached OAuth token to force refresh on next request."""
    global _oauth_access_token, _oauth_token_expires_at
    async with _auth_lock:
        _oauth_access_token = None
        _oauth_token_expires_at = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("service:app", host="0.0.0.0", port=8081, log_level="info")
