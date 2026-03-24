# ARCHIVED — logic migrated to phantom-tide/collectors/notam/notam_collector.py
# This file is kept for reference only. Do not edit.
# Migration date: 2026-03-19

# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Rules-based NOTAM classification and plain-English translation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any


class OperationalCategory(StrEnum):
    """Operational categories inferred from NOTAM text."""

    RUNWAY = "RUNWAY"
    TAXIWAY = "TAXIWAY"
    APRON = "APRON"
    AIRSPACE = "AIRSPACE"
    NAVIGATION = "NAVIGATION"
    OBSTACLE = "OBSTACLE"
    AERODROME = "AERODROME"
    SERVICE = "SERVICE"
    COMMUNICATION = "COMMUNICATION"
    UNKNOWN = "UNKNOWN"


class SeverityLevel(IntEnum):
    """Priority ranking used for filtering and sorting."""

    CANCELLED = -1
    NOISE = 0
    INFO = 1
    ADVISORY = 2
    WARNING = 3
    CRITICAL = 4

    @classmethod
    def from_query(cls, value: str | None) -> SeverityLevel:
        """Parse a query parameter into a severity level."""

        if not value:
            return cls.INFO
        candidate = value.strip().upper()
        try:
            return cls[candidate]
        except KeyError as exc:
            raise ValueError(f"Unsupported severity level: {value}") from exc


@dataclass(frozen=True)
class NotamIntelligence:
    """Derived operational interpretation for one NOTAM row."""

    operational_category: OperationalCategory
    severity: SeverityLevel
    is_noise: bool
    facility: str
    plain_english: str
    expanded_text: str
    signals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""

        return {
            "operational_category": self.operational_category.value,
            "severity": self.severity.name,
            "severity_rank": int(self.severity),
            "is_noise": self.is_noise,
            "facility": self.facility,
            "plain_english": self.plain_english,
            "expanded_text": self.expanded_text,
            "signals": list(self.signals),
        }


_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    ("RWY", "runway"),
    ("TWY", "taxiway"),
    ("AP", "airport"),
    ("AD", "aerodrome"),
    ("CLSD", "closed"),
    ("WIP", "work in progress"),
    ("PJE", "parachute jumping exercise"),
    ("OBST", "obstacle"),
    ("SFC", "surface"),
    ("INOP", "inoperative"),
    ("U/S", "unserviceable"),
    ("OTS", "out of service"),
    ("NAV", "navigation"),
    ("COM", "communication"),
    ("FREQ", "frequency"),
    ("FICON", "field condition"),
    ("WET", "wet"),
    ("SN", "snow"),
    ("RMK", "remark"),
    ("WI", "within"),
    ("BTN", "between"),
    ("BNT", "between"),
    ("FM", "from"),
    ("TIL", "until"),
    ("INTL", "international"),
)

_CATEGORY_RULES: tuple[tuple[OperationalCategory, tuple[str, ...]], ...] = (
    (OperationalCategory.RUNWAY, ("RWY", "RUNWAY", "THR", "THRESHOLD")),
    (OperationalCategory.TAXIWAY, ("TWY", "TAXIWAY", "TAXI ")),
    (OperationalCategory.APRON, ("APRON", "RAMP", "PARKING APRON")),
    (
        OperationalCategory.AIRSPACE,
        (
            "AIRSPACE",
            "TFR",
            "RESTRICTED",
            "PJE",
            "PARACHUTE",
            "LASER",
            "UAS",
            "UAV",
            "DRONE",
            "FDC",
        ),
    ),
    (
        OperationalCategory.NAVIGATION,
        ("VOR", "NDB", "ILS", "DME", "TACAN", "NAV", "GPS", "RNAV"),
    ),
    (
        OperationalCategory.OBSTACLE,
        ("OBST", "OBSTACLE", "CRANE", "TOWER", "WIRE", "POLE"),
    ),
    (
        OperationalCategory.COMMUNICATION,
        ("CTAF", "ATIS", "UNICOM", "RADIO", "FREQ", "FREQUENCY"),
    ),
    (
        OperationalCategory.SERVICE,
        ("FUEL", "ARFF", "DEICE", "BIRD", "PPR", "SERVICE", "SVC"),
    ),
    (
        OperationalCategory.AERODROME,
        ("AERODROME", "AIRPORT", "AD AP", "AERODROME/", "AIRFIELD"),
    ),
)

_ROUTINE_NOISE_TERMS: tuple[str, ...] = (
    "BIRD ACTIVITY",
    "BIRDS INVOF",
    "FUEL AVBL",
    "FUEL AVAILABLE",
    "PPR",
    "PRIOR PERMISSION",
    "HOURS OF OPS",
    "UNMONITORED",
    "PHONE",
    "CONTACT",
)

_CRITICAL_TERMS: tuple[str, ...] = (
    "CLSD",
    "CLOSED",
    "HAZARD",
    "UNSAFE",
    "OBSTRUCTION",
    "BRAKING ACTION NIL",
    "RWYCC 0",
    "TFR",
    "FDC",
)

_WARNING_TERMS: tuple[str, ...] = (
    "INOP",
    "U/S",
    "OTS",
    "OUT OF SERVICE",
    "RESTRICTED",
    "LIMITED",
    "PJE",
    "PARACHUTE",
    "LASER",
    "UAS",
    "UAV",
    "DRONE",
    "OBST",
    "OBSTACLE",
    "CRANE",
    "SNOW",
    "ICE",
    "FICON",
    "FROST",
)

_ADVISORY_TERMS: tuple[str, ...] = (
    "WIP",
    "WORK IN PROGRESS",
    "MAINT",
    "MAINTENANCE",
    "WET",
    "PATCHY",
    "BIRD",
    "CAUTION",
    "MOWING",
    "DEICED",
)


class NotamIntelligenceEngine:
    """Analyze NOTAM rows into ranked operational summaries."""

    def analyze_row(self, row: Mapping[str, Any]) -> NotamIntelligence:
        """Classify and translate one NOTAM row."""

        raw_text = str(row.get("notamtext") or "").strip()
        status = str(row.get("status") or "ACTIVE").strip().upper()
        text_upper = f" {raw_text.upper()} "
        category = self._detect_category(text_upper)
        facility = self._detect_facility(raw_text, category)
        severity, signals = self._assess_severity(text_upper, category, status)
        expanded_text = self._expand_text(raw_text)
        is_noise = self._detect_noise(text_upper, category, severity)
        if is_noise and severity <= SeverityLevel.INFO:
            severity = SeverityLevel.NOISE

        return NotamIntelligence(
            operational_category=category,
            severity=severity,
            is_noise=is_noise,
            facility=facility,
            plain_english=self._build_plain_english(
                row=row,
                category=category,
                facility=facility,
                severity=severity,
                expanded_text=expanded_text,
            ),
            expanded_text=expanded_text,
            signals=signals,
        )

    def enrich_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Return a row plus nested intelligence metadata."""

        intelligence = self.analyze_row(row)
        payload = dict(row)
        payload["intelligence"] = intelligence.to_dict()
        return payload

    def build_response(
        self,
        rows: list[dict[str, Any]],
        *,
        include_noise: bool = False,
        min_severity: SeverityLevel = SeverityLevel.INFO,
    ) -> dict[str, Any]:
        """Build an API response with filtering and summary metadata."""

        enriched_rows = [self.enrich_row(row) for row in rows]
        returned_items: list[dict[str, Any]] = []
        noise_filtered_count = 0
        severity_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}

        for item in enriched_rows:
            intelligence = item["intelligence"]
            severity_name = str(intelligence["severity"])
            category_name = str(intelligence["operational_category"])
            severity_counts[severity_name] = severity_counts.get(severity_name, 0) + 1
            category_counts[category_name] = category_counts.get(category_name, 0) + 1

            if intelligence["is_noise"] and not include_noise:
                noise_filtered_count += 1
                continue
            if int(intelligence["severity_rank"]) < int(min_severity):
                continue
            returned_items.append(item)

        returned_items.sort(key=self._sort_key)
        critical_count = sum(
            1
            for item in returned_items
            if item["intelligence"]["severity"] == SeverityLevel.CRITICAL.name
        )

        return {
            "items": returned_items,
            "summary": {
                "total_analyzed": len(enriched_rows),
                "returned_count": len(returned_items),
                "critical_count": critical_count,
                "noise_filtered_count": noise_filtered_count,
                "severity_counts": severity_counts,
                "category_counts": category_counts,
            },
        }

    def _detect_category(self, text_upper: str) -> OperationalCategory:
        for category, terms in _CATEGORY_RULES:
            if self._contains_any(text_upper, terms):
                return category
        return OperationalCategory.UNKNOWN

    def _detect_facility(self, raw_text: str, category: OperationalCategory) -> str:
        runway_match = re.search(
            r"\bRWY\s+([0-9]{1,2}[LRC]?/[0-9]{1,2}[LRC]?|[0-9]{1,2}[LRC]?)\b",
            raw_text,
            flags=re.IGNORECASE,
        )
        if runway_match:
            return f"runway {runway_match.group(1).upper()}"

        taxiway_match = re.search(
            r"\bTWY\s+([A-Z0-9]{1,6})\b",
            raw_text,
            flags=re.IGNORECASE,
        )
        if taxiway_match:
            return f"taxiway {taxiway_match.group(1).upper()}"

        if category == OperationalCategory.AIRSPACE:
            return "airspace"
        if category == OperationalCategory.NAVIGATION:
            nav_match = re.search(
                r"\b(VOR|NDB|ILS|DME|TACAN|GPS|RNAV)\b",
                raw_text,
                flags=re.IGNORECASE,
            )
            if nav_match:
                return nav_match.group(1).upper()
            return "navigation aid"
        if category == OperationalCategory.OBSTACLE:
            return "obstacle area"
        if category == OperationalCategory.APRON:
            return "apron"
        if category == OperationalCategory.SERVICE:
            return "airport service"
        if category == OperationalCategory.COMMUNICATION:
            return "communication service"
        if category == OperationalCategory.AERODROME:
            return "airport surface"
        return "airport operations"

    def _assess_severity(
        self,
        text_upper: str,
        category: OperationalCategory,
        status: str,
    ) -> tuple[SeverityLevel, tuple[str, ...]]:
        if status != "ACTIVE" or self._contains_any(text_upper, ("CNL", "CANCELLED")):
            return SeverityLevel.CANCELLED, ("CANCELLED",)

        signals = [
            *self._matching_terms(text_upper, _CRITICAL_TERMS),
            *self._matching_terms(text_upper, _WARNING_TERMS),
            *self._matching_terms(text_upper, _ADVISORY_TERMS),
        ]

        if category == OperationalCategory.RUNWAY:
            if self._contains_any(text_upper, ("CLSD", "CLOSED", "FICON")):
                return SeverityLevel.CRITICAL, tuple(dict.fromkeys(signals))
            if self._contains_any(text_upper, ("WIP", "MAINT", "SNOW", "ICE")):
                return SeverityLevel.WARNING, tuple(dict.fromkeys(signals))
        if category == OperationalCategory.TAXIWAY:
            if self._contains_any(text_upper, ("CLSD", "CLOSED")):
                return SeverityLevel.WARNING, tuple(dict.fromkeys(signals))
            if self._contains_any(text_upper, ("FICON", "SNOW", "ICE", "WET")):
                return SeverityLevel.ADVISORY, tuple(dict.fromkeys(signals))
        if category == OperationalCategory.AIRSPACE:
            if self._contains_any(text_upper, ("TFR", "FDC")):
                return SeverityLevel.CRITICAL, tuple(dict.fromkeys(signals))
            if self._contains_any(
                text_upper,
                ("PJE", "PARACHUTE", "LASER", "UAS", "UAV", "DRONE"),
            ):
                return SeverityLevel.WARNING, tuple(dict.fromkeys(signals))
        if category in {
            OperationalCategory.NAVIGATION,
            OperationalCategory.COMMUNICATION,
        }:
            if self._contains_any(text_upper, ("INOP", "U/S", "OTS", "OUT OF SERVICE")):
                return SeverityLevel.WARNING, tuple(dict.fromkeys(signals))
        if category == OperationalCategory.OBSTACLE:
            if self._contains_any(
                text_upper, ("CRANE", "OBST", "OBSTACLE", "WIRE", "TOWER")
            ):
                return SeverityLevel.WARNING, tuple(dict.fromkeys(signals))

        if self._contains_any(text_upper, _CRITICAL_TERMS):
            return SeverityLevel.CRITICAL, tuple(dict.fromkeys(signals))
        if self._contains_any(text_upper, _WARNING_TERMS):
            return SeverityLevel.WARNING, tuple(dict.fromkeys(signals))
        if self._contains_any(text_upper, _ADVISORY_TERMS):
            return SeverityLevel.ADVISORY, tuple(dict.fromkeys(signals))
        return SeverityLevel.INFO, tuple(dict.fromkeys(signals))

    def _detect_noise(
        self,
        text_upper: str,
        category: OperationalCategory,
        severity: SeverityLevel,
    ) -> bool:
        # Treat as noise if severity is below WARNING
        if severity >= SeverityLevel.WARNING:
            return False

        # Routine noise terms
        if self._contains_any(text_upper, _ROUTINE_NOISE_TERMS):
            return True

        # Special case: Runway/Taxiway surface condition NOTAMs (e.g., snow, ice, wet, FICON)
        if category in {OperationalCategory.RUNWAY, OperationalCategory.TAXIWAY}:
            if self._contains_any(text_upper, ("FICON", "SNOW", "ICE", "WET", "FROST")):
                # Only treat as noise if not also closed/critical
                if not self._contains_any(text_upper, ("CLSD", "CLOSED")):
                    return True

        # Service/Communication low-severity are noise
        return (
            category
            in {
                OperationalCategory.SERVICE,
                OperationalCategory.COMMUNICATION,
            }
            and severity <= SeverityLevel.INFO
        )

    def _expand_text(self, raw_text: str) -> str:
        text = re.sub(r"\s+", " ", raw_text).strip()
        for abbreviation, expansion in _ABBREVIATIONS:
            pattern = re.compile(
                rf"(?<![A-Z0-9]){re.escape(abbreviation)}(?![A-Z0-9])",
                flags=re.IGNORECASE,
            )
            text = pattern.sub(expansion, text)
        text = re.sub(r"\s+", " ", text).strip(" .")
        if not text:
            return "No NOTAM text available"
        return text[0].upper() + text[1:]

    def _build_plain_english(
        self,
        *,
        row: Mapping[str, Any],
        category: OperationalCategory,
        facility: str,
        severity: SeverityLevel,
        expanded_text: str,
    ) -> str:
        location = str(row.get("locationdesignator") or "unknown location").upper()
        start_text = self._format_timestamp(row.get("validfromtimestamp"))
        end_text = self._format_timestamp(row.get("validtotimestamp"))
        raw_text = str(row.get("notamtext") or "")
        text_upper = raw_text.upper()

        if severity == SeverityLevel.CANCELLED:
            return f"At {location}, this NOTAM has been cancelled."

        if category == OperationalCategory.RUNWAY and self._contains_any(
            text_upper, ("CLSD", "CLOSED")
        ):
            return (
                f"At {location}, {facility} is closed"
                f"{self._time_phrase(start_text, end_text)}."
            )
        if category == OperationalCategory.TAXIWAY and self._contains_any(
            text_upper, ("CLSD", "CLOSED")
        ):
            return (
                f"At {location}, {facility} is closed"
                f"{self._time_phrase(start_text, end_text)}."
            )
        if category == OperationalCategory.AIRSPACE and self._contains_any(
            text_upper, ("PJE", "PARACHUTE")
        ):
            return (
                f"At {location}, parachute activity is active in the airspace"
                f"{self._time_phrase(start_text, end_text)}."
            )
        if category == OperationalCategory.AIRSPACE and self._contains_any(
            text_upper, ("TFR", "FDC")
        ):
            return (
                f"At {location}, a flight restriction is in effect"
                f"{self._time_phrase(start_text, end_text)}."
            )
        if category in {
            OperationalCategory.NAVIGATION,
            OperationalCategory.COMMUNICATION,
        } and self._contains_any(text_upper, ("INOP", "U/S", "OTS", "OUT OF SERVICE")):
            return (
                f"At {location}, {facility} is unavailable"
                f"{self._time_phrase(start_text, end_text)}."
            )
        if self._contains_any(
            text_upper, ("WIP", "WORK IN PROGRESS", "MAINT", "MAINTENANCE")
        ):
            return (
                f"At {location}, work is underway affecting {facility}"
                f"{self._time_phrase(start_text, end_text)}."
            )
        if self._contains_any(text_upper, ("FICON", "SNOW", "ICE", "WET", "FROST")):
            return (
                f"At {location}, {facility} has degraded surface conditions"
                f"{self._time_phrase(start_text, end_text)}."
            )
        if severity == SeverityLevel.NOISE:
            return f"At {location}, this is a low-priority routine notice: {expanded_text}."
        return f"At {location}, operational attention is required for {facility}: {expanded_text}."

    def _contains_any(self, text_upper: str, terms: tuple[str, ...]) -> bool:
        return any(self._contains_term(text_upper, term) for term in terms)

    def _matching_terms(self, text_upper: str, terms: tuple[str, ...]) -> list[str]:
        return [term for term in terms if self._contains_term(text_upper, term)]

    def _contains_term(self, text_upper: str, term: str) -> bool:
        return (
            re.search(
                rf"(?<![A-Z0-9]){re.escape(term)}(?![A-Z0-9])",
                text_upper,
            )
            is not None
        )

    def _format_timestamp(self, value: Any) -> str | None:
        parsed = self._coerce_datetime(value)
        if parsed is None:
            return None
        return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    def _coerce_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        if isinstance(value, str) and value:
            candidate = value.strip()
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        return None

    def _time_phrase(self, start_text: str | None, end_text: str | None) -> str:
        if start_text and end_text:
            return f" from {start_text} until {end_text}"
        if end_text:
            return f" until {end_text}"
        if start_text:
            return f" starting {start_text}"
        return ""

    def _sort_key(self, item: Mapping[str, Any]) -> tuple[int, float, int]:
        intelligence = item["intelligence"]
        valid_to = self._coerce_datetime(item.get("validtotimestamp"))
        if valid_to is None:
            valid_to_rank = float("inf")
        else:
            valid_to_rank = valid_to.timestamp()
        return (
            -int(intelligence["severity_rank"]),
            valid_to_rank,
            int(item.get("fnsid") or 0),
        )
