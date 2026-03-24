# ARCHIVED — logic migrated to phantom-tide/collectors/notam/notam_collector.py
# This file is kept for reference only. Do not edit.
# Migration date: 2026-03-19

# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""AIXM/FNS message models and parsing helpers."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum


class NotamStatus(StrEnum):
    """Supported NOTAM states."""

    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class FnsMessageParseError(ValueError):
    """Raised when an AIXM message cannot be parsed into a NOTAM."""


@dataclass(frozen=True)
class FnsMessage:
    """One parsed FNS message."""

    fns_id: int
    correlation_id: int
    issued_timestamp: datetime | None
    updated_timestamp: datetime | None
    valid_from_timestamp: datetime | None
    valid_to_timestamp: datetime | None
    classification: str
    location_designator: str
    notam_accountability: str
    notam_text: str
    aixm_notam_message: str
    status: NotamStatus = NotamStatus.ACTIVE

    def to_record(self) -> dict[str, object]:
        """Return a serializable database payload."""

        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_xml(
        cls,
        correlation_id: int,
        xml_message: str,
        *,
        status: NotamStatus = NotamStatus.ACTIVE,
    ) -> FnsMessage:
        """Parse an AIXM message into a NOTAM record."""

        try:
            root = ET.fromstring(xml_message.strip())
        except ET.ParseError as exc:
            raise FnsMessageParseError("invalid XML message") from exc

        message_id = _first_attribute_value(root, {"id"})
        if not message_id:
            raise FnsMessageParseError("AIXM message is missing an identifier")

        id_match = re.search(r"(\d+)$", message_id)
        if id_match is None:
            raise FnsMessageParseError(f"unable to parse FNS id from {message_id!r}")

        event_time_slice = _find_notam_event_time_slice(root)
        if event_time_slice is None:
            raise FnsMessageParseError("message did not contain a NOTAM event")

        notam_node = _find_first(event_time_slice, {"textNOTAM"})
        if notam_node is None or len(notam_node) == 0:
            raise FnsMessageParseError("message did not contain a textNOTAM node")
        notam = next(iter(notam_node))

        extension_parent = _find_first(event_time_slice, {"extension"})
        extension = (
            next(iter(extension_parent))
            if extension_parent is not None and len(extension_parent) > 0
            else None
        )

        valid_time = _find_first(event_time_slice, {"validTime"})
        time_period = None
        if valid_time is not None:
            abstract_time = _find_first(
                valid_time, {"TimePeriod", "AbstractTimePrimitive"}
            )
            time_period = abstract_time if abstract_time is not None else valid_time

        classification = _find_first_text(extension, {"classification"})
        effective_end = _find_first_text(notam, {"effectiveEnd"})
        valid_to = _parse_timestamp(_find_first_text(time_period, {"endPosition"}))
        if (
            classification in {"INTL", "MIL", "LMIL"}
            and effective_end
            and "EST" in effective_end
        ):
            valid_to = None

        return cls(
            fns_id=int(id_match.group(1)),
            correlation_id=correlation_id,
            issued_timestamp=_parse_timestamp(_find_first_text(notam, {"issued"})),
            updated_timestamp=_parse_timestamp(
                _find_first_text(extension, {"lastUpdated"})
            ),
            valid_from_timestamp=_parse_timestamp(
                _find_first_text(time_period, {"beginPosition"})
            ),
            valid_to_timestamp=valid_to,
            classification=classification or "",
            location_designator=_find_first_text(notam, {"location"}) or "",
            notam_accountability=_find_first_text(extension, {"accountId"}) or "",
            notam_text=_find_first_text(notam, {"text"}) or "",
            aixm_notam_message=xml_message.strip(),
            status=status,
        )


def strip_xml_declaration(xml_message: str) -> str:
    """Remove an XML declaration so messages can be concatenated safely."""

    return re.sub(r"^\s*<\?xml[^>]+\?>", "", xml_message).strip()


def wrap_messages_as_xml(messages: list[str]) -> str:
    """Wrap one or more AIXM messages in the legacy collection element."""

    body = "".join(strip_xml_declaration(message) for message in messages)
    return f"<AixmBasicMessageCollection>{body}</AixmBasicMessageCollection>"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_nodes(root: ET.Element | None):
    if root is None:
        return
    yield root
    for child in root.iter():
        if child is not root:
            yield child


def _find_first(root: ET.Element | None, names: set[str]) -> ET.Element | None:
    for node in _iter_nodes(root):
        if _local_name(node.tag) in names:
            return node
    return None


def _find_first_text(root: ET.Element | None, names: set[str]) -> str | None:
    node = _find_first(root, names)
    if node is None:
        return None
    text = "".join(node.itertext()).strip()
    return text or None


def _first_attribute_value(root: ET.Element, names: set[str]) -> str | None:
    for key, value in root.attrib.items():
        if _local_name(key) in names and value:
            return value
    return None


def _find_notam_event_time_slice(root: ET.Element) -> ET.Element | None:
    for node in root.iter():
        if _local_name(node.tag) != "EventTimeSlice":
            continue
        text_notam = _find_first(node, {"textNOTAM"})
        if text_notam is not None and len(text_notam) > 0:
            return node
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    for parser in (datetime.fromisoformat,):
        try:
            parsed = parser(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(candidate, fmt).astimezone(UTC)
        except ValueError:
            continue
    return None
