"""Tests for database persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fns_client.config import DatabaseConfig
from fns_client.database import NotamDb
from fns_client.messages import FnsMessage, NotamStatus


def test_notam_db_can_insert_and_query(tmp_path) -> None:
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
            fns_id=1001,
            correlation_id=1,
            issued_timestamp=now - timedelta(minutes=5),
            updated_timestamp=now - timedelta(minutes=4),
            valid_from_timestamp=now - timedelta(minutes=1),
            valid_to_timestamp=now + timedelta(hours=6),
            classification="DOM",
            location_designator="KATL",
            notam_accountability="KZTL",
            notam_text="RWY 09/27 CLOSED",
            aixm_notam_message="<AIXMBasicMessage />",
            status=NotamStatus.ACTIVE,
        )
    )

    results = db.get_by_location_designator("KATL")
    assert results == ["<AIXMBasicMessage />"]


def test_list_notams_defaults_to_recent_active_rows(tmp_path) -> None:
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
            fns_id=2001,
            correlation_id=1,
            issued_timestamp=now - timedelta(days=2, minutes=5),
            updated_timestamp=now - timedelta(days=2, minutes=4),
            valid_from_timestamp=now - timedelta(days=2),
            valid_to_timestamp=now + timedelta(hours=6),
            classification="DOM",
            location_designator="KATL",
            notam_accountability="KZTL",
            notam_text="TWY A CLOSED",
            aixm_notam_message="<AIXMBasicMessage />",
            status=NotamStatus.ACTIVE,
        )
    )
    db.put_notam(
        FnsMessage(
            fns_id=2002,
            correlation_id=2,
            issued_timestamp=now - timedelta(minutes=5),
            updated_timestamp=now - timedelta(minutes=4),
            valid_from_timestamp=now - timedelta(minutes=1),
            valid_to_timestamp=now + timedelta(hours=6),
            classification="DOM",
            location_designator="KATL",
            notam_accountability="KZTL",
            notam_text="RWY 09/27 CLOSED",
            aixm_notam_message="<AIXMBasicMessage />",
            status=NotamStatus.ACTIVE,
        )
    )

    default_rows = db.list_notams()
    archived_rows = db.list_notams(include_archived=True)

    assert [row["fnsid"] for row in default_rows] == [2002]
    assert [row["fnsid"] for row in archived_rows] == [2001, 2002]
