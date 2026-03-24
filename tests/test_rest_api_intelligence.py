"""Tests for the NOTAM intelligence REST endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from fns_client.config import DatabaseConfig
from fns_client.database import NotamDb
from fns_client.messages import FnsMessage, NotamStatus
from fns_client.rest_api import create_rest_app


def test_api_notams_intelligence_filters_noise_and_returns_summary(tmp_path) -> None:
    now = datetime.now(UTC)
    db = NotamDb(
        DatabaseConfig(
            connection_url=f"sqlite:///{tmp_path / 'notams.sqlite3'}",
            table="NOTAMS",
            schema="PUBLIC",
        )
    )
    db.create_notam_table()
    db.set_valid()

    db.put_notam(
        FnsMessage(
            fns_id=3001,
            correlation_id=1,
            issued_timestamp=now - timedelta(minutes=5),
            updated_timestamp=now - timedelta(minutes=4),
            valid_from_timestamp=now,
            valid_to_timestamp=now + timedelta(hours=3),
            classification="DOM",
            location_designator="KATL",
            notam_accountability="KZTL",
            notam_text="RWY 09/27 CLSD DUE TO MAINTENANCE",
            aixm_notam_message="<AIXMBasicMessage />",
            status=NotamStatus.ACTIVE,
        )
    )
    db.put_notam(
        FnsMessage(
            fns_id=3002,
            correlation_id=2,
            issued_timestamp=now - timedelta(minutes=5),
            updated_timestamp=now - timedelta(minutes=4),
            valid_from_timestamp=now,
            valid_to_timestamp=now + timedelta(hours=3),
            classification="DOM",
            location_designator="KATL",
            notam_accountability="KZTL",
            notam_text="FUEL AVBL 0800-1700 PHONE OPS",
            aixm_notam_message="<AIXMBasicMessage />",
            status=NotamStatus.ACTIVE,
        )
    )

    client = TestClient(create_rest_app(db))

    response = client.get("/api/notams/intelligence")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total_analyzed"] == 2
    assert payload["summary"]["noise_filtered_count"] == 1
    assert payload["summary"]["critical_count"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["fnsid"] == 3001
    assert payload["items"][0]["intelligence"]["severity"] == "CRITICAL"
    assert "closed" in payload["items"][0]["intelligence"]["plain_english"].lower()


def test_api_notams_can_include_inline_intelligence(tmp_path) -> None:
    now = datetime.now(UTC)
    db = NotamDb(
        DatabaseConfig(
            connection_url=f"sqlite:///{tmp_path / 'notams.sqlite3'}",
            table="NOTAMS",
            schema="PUBLIC",
        )
    )
    db.create_notam_table()
    db.set_valid()

    db.put_notam(
        FnsMessage(
            fns_id=3003,
            correlation_id=1,
            issued_timestamp=now - timedelta(minutes=5),
            updated_timestamp=now - timedelta(minutes=4),
            valid_from_timestamp=now,
            valid_to_timestamp=now + timedelta(hours=2),
            classification="DOM",
            location_designator="KATL",
            notam_accountability="KZTL",
            notam_text="TWY A CLSD",
            aixm_notam_message="<AIXMBasicMessage />",
            status=NotamStatus.ACTIVE,
        )
    )

    client = TestClient(create_rest_app(db))

    response = client.get("/api/notams?include_intelligence=true")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["intelligence"]["operational_category"] == "TAXIWAY"
    assert payload["items"][0]["intelligence"]["severity"] == "WARNING"
