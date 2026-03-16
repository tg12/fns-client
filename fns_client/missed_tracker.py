# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Missed/stale message tracking."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from fns_client.config import MessageTrackerConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackedMessage:
    """A message correlation id that has not arrived yet."""

    correlation_id: int
    first_missing_at: datetime


class MissedMessageTracker:
    """Track gaps in message ids and periods of complete message silence."""

    def __init__(
        self,
        config: MessageTrackerConfig,
        *,
        on_missed: Callable[[dict[int, datetime]], None],
        on_stale: Callable[[int | None, datetime | None], None],
    ) -> None:
        self._config: Final = config
        self._on_missed = on_missed
        self._on_stale = on_stale
        self._lock = threading.Lock()
        self._missing_messages: dict[int, datetime] = {}
        self._last_received_id: int | None = None
        self._last_received_at: datetime | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start periodic tracking checks."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="missed-message-tracker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop periodic tracking checks."""

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def clear_all_messages(self) -> None:
        """Reset tracked state."""

        with self._lock:
            self._missing_messages.clear()
            self._last_received_id = None
            self._last_received_at = None

    def clear_only_missed_messages(self) -> None:
        """Forget only missing ids while preserving stale tracking."""

        with self._lock:
            self._missing_messages.clear()

    def set_last_received_tracking_id(self, correlation_id: int) -> None:
        """Seed the tracker from existing state in the database."""

        with self._lock:
            self._last_received_id = correlation_id
            self._last_received_at = datetime.now(UTC)

    def put(self, correlation_id: int, received_at: datetime) -> None:
        """Record a newly received correlation id."""

        if correlation_id < 0:
            return
        with self._lock:
            if (
                self._last_received_id is not None
                and correlation_id > self._last_received_id + 1
            ):
                for missing_id in range(self._last_received_id + 1, correlation_id):
                    self._missing_messages.setdefault(missing_id, received_at)
            self._missing_messages.pop(correlation_id, None)
            self._last_received_id = correlation_id
            self._last_received_at = received_at

    def _run(self) -> None:
        interval = max(self._config.schedule_rate_seconds, 1)
        while not self._stop_event.wait(interval):
            self._check_missing_messages()
            self._check_stale_state()

    def _check_missing_messages(self) -> None:
        threshold = timedelta(minutes=self._config.missed_message_trigger_time_minutes)
        now = datetime.now(UTC)
        to_report: dict[int, datetime] = {}
        with self._lock:
            for correlation_id, first_missing_at in self._missing_messages.items():
                if now - first_missing_at >= threshold:
                    to_report[correlation_id] = first_missing_at
        if to_report:
            LOGGER.warning("Missed messages detected ids=%s", sorted(to_report))
            self._on_missed(to_report)

    def _check_stale_state(self) -> None:
        threshold = timedelta(minutes=self._config.stale_message_trigger_time_minutes)
        with self._lock:
            last_received_id = self._last_received_id
            last_received_at = self._last_received_at
        if last_received_at is None:
            return
        if datetime.now(UTC) - last_received_at >= threshold:
            LOGGER.warning(
                "No messages received since %s", last_received_at.isoformat()
            )
            self._on_stale(last_received_id, last_received_at)
