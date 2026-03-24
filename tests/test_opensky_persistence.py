"""Tests for the local OpenSky persistence store."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_persistence_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "opensky_poller" / "persistence.py"
    )
    spec = importlib.util.spec_from_file_location(
        "opensky_persistence_module", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_state(
    *,
    icao24: str,
    callsign: str,
    lat: float,
    lon: float,
    last_contact: int,
) -> dict[str, object]:
    return {
        "icao24": icao24,
        "callsign": callsign,
        "origin_country": "United States",
        "time_position": last_contact,
        "last_contact": last_contact,
        "longitude": lon,
        "latitude": lat,
        "baro_altitude": 1200.0,
        "on_ground": False,
        "velocity": 140.0,
        "true_track": 180.0,
        "vertical_rate": 0.0,
        "geo_altitude": 1300.0,
        "squawk": "1200",
        "position_source": 0,
        "category": 3,
    }


def test_local_observation_store_exposes_batches_current_history_and_payload(
    tmp_path: Path,
) -> None:
    persistence = _load_persistence_module()
    store = persistence.LocalObservationStore(tmp_path / "opensky")
    store.initialize()

    first_states = [
        _sample_state(
            icao24="abc123",
            callsign="TEST123",
            lat=33.64,
            lon=-84.43,
            last_contact=1_700_000_000,
        ),
        _sample_state(
            icao24="def456",
            callsign="TEST456",
            lat=33.65,
            lon=-84.44,
            last_contact=1_700_000_000,
        ),
    ]
    second_states = [
        _sample_state(
            icao24="abc123",
            callsign="TEST123",
            lat=33.70,
            lon=-84.40,
            last_contact=1_700_000_060,
        )
    ]
    bbox = {"lamin": 33.0, "lomin": -85.0, "lamax": 34.0, "lomax": -84.0}

    store.record_poll(
        polled_at=1_700_000_000,
        status_code=200,
        success=True,
        auth_mode="oauth2",
        bbox=bbox,
        bbox_area_sqdeg=1.0,
        latency_ms=125.0,
        rate_limit_remaining=3998,
        retry_after_seconds=None,
        estimated_credit_cost=1,
        aircraft_states=first_states,
        raw_payload={"time": 1_700_000_000, "states": first_states},
        error_text=None,
    )
    store.record_poll(
        polled_at=1_700_000_060,
        status_code=200,
        success=True,
        auth_mode="oauth2",
        bbox=bbox,
        bbox_area_sqdeg=1.0,
        latency_ms=130.0,
        rate_limit_remaining=3997,
        retry_after_seconds=None,
        estimated_credit_cost=1,
        aircraft_states=second_states,
        raw_payload={"time": 1_700_000_060, "states": second_states},
        error_text=None,
    )

    batches = store.list_poll_batches(limit=5)
    assert len(batches) == 2
    assert batches[0]["success"] is True
    assert batches[0]["payload_available"] is True
    assert str(batches[0]["payload_path"]).endswith(".json.gz")

    latest_batch_id = int(batches[0]["id"])
    payload = store.get_batch_payload(latest_batch_id)
    assert payload is not None
    assert payload["batch"]["id"] == latest_batch_id
    assert payload["payload"]["states"][0]["icao24"] == "abc123"

    current = store.list_current_aircraft(limit=10)
    assert len(current) == 2
    current_by_icao = {item["icao24"]: item for item in current}
    assert current_by_icao["abc123"]["visibility_status"] == "seen"
    assert current_by_icao["def456"]["visibility_status"] == "stale_unseen"
    assert current_by_icao["abc123"]["state"]["latitude"] == 33.70

    stale_only = store.list_current_aircraft(
        limit=10,
        visibility_status="stale_unseen",
    )
    assert [item["icao24"] for item in stale_only] == ["def456"]

    history = store.list_aircraft_history(icao24="abc123", limit=10)
    assert len(history) >= 2
    assert history[0]["event_type"] == "state_changed"
    assert history[-1]["event_type"] == "first_seen"

    stale_history = store.list_aircraft_history(icao24="def456", limit=10)
    assert stale_history[0]["event_type"] == "stale_unseen"
