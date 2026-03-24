"""Tests for NOTAM intelligence classification and translation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fns_client.notam_intelligence import (
    NotamIntelligenceEngine,
    OperationalCategory,
    SeverityLevel,
)


def _base_row(**overrides):
    now = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
    row = {
        "fnsid": 1001,
        "locationdesignator": "KATL",
        "classification": "DOM",
        "notamaccountability": "KZTL",
        "notamtext": "RWY 09/27 CLSD",
        "status": "ACTIVE",
        "validfromtimestamp": now,
        "validtotimestamp": now + timedelta(hours=6),
    }
    row.update(overrides)
    return row


def test_runway_closure_is_classified_as_critical() -> None:
    engine = NotamIntelligenceEngine()

    result = engine.analyze_row(
        _base_row(notamtext="RWY 09/27 CLSD DUE TO MAINTENANCE")
    )

    assert result.operational_category == OperationalCategory.RUNWAY
    assert result.severity == SeverityLevel.CRITICAL
    assert result.is_noise is False
    assert result.facility == "runway 09/27"
    assert "runway 09/27 is closed" in result.plain_english.lower()


def test_routine_service_notice_is_marked_as_noise() -> None:
    engine = NotamIntelligenceEngine()

    result = engine.analyze_row(
        _base_row(
            fnsid=1002,
            notamtext="FUEL AVBL 0800-1700 PHONE OPS FOR DETAILS",
            classification="DOM",
        )
    )

    assert result.operational_category == OperationalCategory.SERVICE
    assert result.severity == SeverityLevel.NOISE
    assert result.is_noise is True
    assert "low-priority routine notice" in result.plain_english.lower()


def test_airspace_activity_is_translated_to_plain_english() -> None:
    engine = NotamIntelligenceEngine()

    result = engine.analyze_row(
        _base_row(
            fnsid=1003,
            locationdesignator="KMFR",
            notamtext=(
                "AIRSPACE PJE WI AN AREA DEFINED AS 3NM RADIUS OF OED345004 SFC-14000FT"
            ),
        )
    )

    assert result.operational_category == OperationalCategory.AIRSPACE
    assert result.severity == SeverityLevel.WARNING
    assert (
        "parachute activity is active in the airspace" in result.plain_english.lower()
    )


def test_build_response_filters_noise_and_sorts_by_severity() -> None:
    engine = NotamIntelligenceEngine()
    rows = [
        _base_row(fnsid=2001, notamtext="FUEL AVBL 0800-1700 PHONE OPS"),
        _base_row(fnsid=2002, notamtext="RWY 09/27 CLSD"),
        _base_row(fnsid=2003, notamtext="TWY A CLSD"),
    ]

    payload = engine.build_response(rows, include_noise=False)

    assert payload["summary"]["total_analyzed"] == 3
    assert payload["summary"]["noise_filtered_count"] == 1
    assert [item["fnsid"] for item in payload["items"]] == [2002, 2003]
