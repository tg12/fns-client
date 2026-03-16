# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Main service orchestration for the Python FNS client."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from fns_client.config import AppConfig
from fns_client.database import NotamDb
from fns_client.fil import FilClient
from fns_client.messages import FnsMessage, FnsMessageParseError, NotamStatus
from fns_client.missed_tracker import MissedMessageTracker
from fns_client.parser import iter_aixm_messages
from fns_client.rest_api import create_rest_app
from fns_client.transport import MessageEnvelope, MessageSource, SolaceMessageSource

LOGGER = logging.getLogger(__name__)


class FnsClientService:
    """Run the Python reimplementation of the FNS client."""

    def __init__(self, config: AppConfig, *, replay_path: Path | None = None) -> None:
        self._config = config
        self._replay_path = replay_path
        self._fil_client = FilClient(config.fil)
        self._notam_db = NotamDb(config.database)
        self._pending_messages: deque[FnsMessage] = deque()
        self._stop_event = threading.Event()
        self._reinitialize_lock = threading.Lock()
        self._message_source: MessageSource | None = self._build_message_source(
            replay_path
        )
        self._consumer_thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._api_server: uvicorn.Server | None = None
        self._api_thread: threading.Thread | None = None
        self._missed_message_during_initialization = False
        self._tracker = MissedMessageTracker(
            config.message_tracker,
            on_missed=self._on_missed_messages,
            on_stale=self._on_stale_messages,
        )

    def start(self) -> None:
        """Start live ingestion and the REST API."""

        LOGGER.info("Starting FnsClientService")
        if not self._notam_db.notam_table_exists():
            self._notam_db.create_notam_table()

        last_correlation = self._notam_db.get_last_correlation_id()
        if last_correlation is not None:
            correlation_id, stored_at = last_correlation
            age_minutes = (
                datetime.now(UTC) - _as_utc_datetime(stored_at)
            ).total_seconds() / 60
            if (
                age_minutes
                < self._config.message_tracker.missed_message_trigger_time_minutes - 1
            ):
                self._notam_db.set_valid()
                self._tracker.set_last_received_tracking_id(correlation_id)
                LOGGER.info(
                    "Recent correlation id found in database. Skipping FIL bootstrap."
                )

        self._tracker.start()
        self._start_message_consumer()
        self._start_cleanup_loop()

        if not self._notam_db.is_valid():
            if self._replay_path is not None or self._can_bootstrap_from_fil():
                self.initialize_notam_db()
            else:
                LOGGER.info(
                    "FIL credentials not configured. Starting in JMS-only mode without initial FIL bootstrap."
                )
                self._notam_db.set_valid()

        if self._config.rest_api.enabled:
            self._start_rest_api()

    def wait_forever(self) -> None:
        """Block until the service is stopped."""

        while not self._stop_event.wait(1):
            continue

    def stop(self) -> None:
        """Stop all running background work."""

        LOGGER.info("Stopping FnsClientService")
        self._stop_event.set()
        self._tracker.stop()
        if self._message_source is not None:
            self._message_source.close()
        if self._api_server is not None:
            self._api_server.should_exit = True
        if self._consumer_thread is not None:
            self._consumer_thread.join(timeout=5)
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5)
        if self._api_thread is not None:
            self._api_thread.join(timeout=5)
        self._fil_client.close()

    def initialize_notam_db_from_fil(self) -> None:
        """Rebuild the local NOTAM database from FIL."""

        with self._reinitialize_lock:
            successful = False
            while not successful and not self._stop_event.is_set():
                LOGGER.info("Initializing database from FIL")
                self._tracker.clear_all_messages()
                ref_datetime = datetime.now(UTC)
                if self._notam_db.is_initializing():
                    return
                self._notam_db.set_initializing(True)
                self._missed_message_during_initialization = False
                try:
                    self._notam_db.drop_notam_table()
                    self._notam_db.create_notam_table()
                    count = 0
                    with self._fil_client.open_initial_load(ref_datetime) as stream:
                        for xml_message in iter_aixm_messages(stream):
                            notam_message = FnsMessage.from_xml(
                                -1, xml_message, status=NotamStatus.ACTIVE
                            )
                            self._notam_db.put_notam(notam_message)
                            count += 1
                    LOGGER.info("Loaded %s NOTAMs from FIL", count)
                    if self._missed_message_during_initialization:
                        LOGGER.error(
                            "Missed message detected during initialization. Repeating FIL bootstrap."
                        )
                        self._pending_messages.clear()
                    else:
                        self._load_queued_messages()
                        self._notam_db.set_valid()
                        successful = True
                        LOGGER.info("Database initialization complete")
                except Exception as exc:
                    LOGGER.error("FIL initialization failed: %s", exc, exc_info=True)
                    time.sleep(5)
                finally:
                    self._notam_db.set_initializing(False)
                    self._fil_client.close()

    def initialize_notam_db(self) -> None:
        """Initialize the database from local replay data or FIL."""

        if self._replay_path is not None:
            self.initialize_notam_db_from_replay()
            return
        if not self._can_bootstrap_from_fil():
            LOGGER.warning(
                "FIL not configured. Cannot reinitialize. Keeping existing data."
            )
            self._load_queued_messages()
            self._notam_db.set_valid()
            return
        self.initialize_notam_db_from_fil()

    def initialize_notam_db_from_replay(self) -> None:
        """Rebuild the local NOTAM database from replay XML files."""

        replay_path = self._replay_path
        if replay_path is None:
            raise ValueError("Replay initialization requires a replay path")

        with self._reinitialize_lock:
            LOGGER.info("Initializing database from replay path=%s", replay_path)
            self._tracker.clear_all_messages()
            self._notam_db.set_initializing(True)
            try:
                self._notam_db.drop_notam_table()
                self._notam_db.create_notam_table()
                count = 0
                correlation_id = 1
                for file_path in self._iter_replay_files(replay_path):
                    notam_message = FnsMessage.from_xml(
                        correlation_id,
                        file_path.read_text(encoding="utf-8"),
                        status=NotamStatus.ACTIVE,
                    )
                    self._notam_db.put_notam(notam_message)
                    count += 1
                    correlation_id += 1
                self._notam_db.set_valid()
                LOGGER.info("Loaded %s NOTAMs from replay data", count)
            finally:
                self._notam_db.set_initializing(False)

    def validate_notam_db(self) -> bool:
        """Compare FIL timestamps with the local database state."""

        if self._replay_path is not None:
            LOGGER.info("Validating database against replay data")
            replay_validation_map: dict[str, datetime | None] = {}
            for correlation_id, file_path in enumerate(
                self._iter_replay_files(self._replay_path), start=1
            ):
                notam_message = FnsMessage.from_xml(
                    correlation_id,
                    file_path.read_text(encoding="utf-8"),
                    status=NotamStatus.ACTIVE,
                )
                replay_validation_map[str(notam_message.fns_id)] = (
                    notam_message.updated_timestamp
                )
            db_validation_map = self._notam_db.get_validation_map()
            for fns_id, replay_timestamp in replay_validation_map.items():
                db_timestamp = db_validation_map.get(fns_id)
                if db_timestamp is None:
                    return False
                if (
                    replay_timestamp
                    and db_timestamp
                    and replay_timestamp > _as_utc_datetime(db_timestamp)
                ):
                    return False
            return True

        LOGGER.info("Validating database against FIL")
        fil_validation_map: dict[str, datetime | None] = {}
        try:
            with self._fil_client.open_initial_load(datetime.now(UTC)) as stream:
                for xml_message in iter_aixm_messages(stream):
                    notam_message = FnsMessage.from_xml(
                        -1, xml_message, status=NotamStatus.ACTIVE
                    )
                    fil_validation_map[str(notam_message.fns_id)] = (
                        notam_message.updated_timestamp
                    )
            db_validation_map = self._notam_db.get_validation_map()
            for fns_id, fil_timestamp in fil_validation_map.items():
                db_timestamp = db_validation_map.get(fns_id)
                if db_timestamp is None:
                    return False
                if (
                    fil_timestamp
                    and db_timestamp
                    and fil_timestamp > _as_utc_datetime(db_timestamp)
                ):
                    return False
            return True
        finally:
            self._fil_client.close()

    def _build_message_source(self, replay_path: Path | None) -> MessageSource | None:
        if replay_path is not None:
            return None
        if self._config.jms.enabled:
            return SolaceMessageSource(self._config.jms)
        return None

    def _can_bootstrap_from_fil(self) -> bool:
        """Return whether enough FIL configuration exists for an initial sync."""

        return all(
            [
                self._config.fil.host.strip(),
                self._config.fil.username.strip(),
                self._config.fil.cert_file_path.strip(),
            ]
        )

    def _start_message_consumer(self) -> None:
        if self._message_source is None:
            LOGGER.info(
                "JMS source disabled. Running FIL/bootstrap and REST modes only."
            )
            return
        self._consumer_thread = threading.Thread(
            target=self._consume_messages, name="message-consumer", daemon=True
        )
        self._consumer_thread.start()

    def _consume_messages(self) -> None:
        assert self._message_source is not None
        for envelope in self._message_source.iter_messages():
            if self._stop_event.is_set():
                break
            self._handle_message(envelope)

    def _handle_message(self, envelope: MessageEnvelope) -> None:
        try:
            message = FnsMessage.from_xml(
                envelope.correlation_id,
                envelope.payload,
                status=NotamStatus(envelope.status.upper()),
            )
        except (FnsMessageParseError, ValueError) as exc:
            LOGGER.error("Failed to parse JMS message: %s", exc, exc_info=True)
            return

        self._tracker.put(message.correlation_id, datetime.now(UTC))

        if not self._notam_db.is_valid():
            LOGGER.debug(
                "Queueing NOTAM while database is invalid fns_id=%s", message.fns_id
            )
            self._pending_messages.append(message)
            return

        try:
            self._notam_db.put_notam(message)
        except Exception as exc:
            LOGGER.warning(
                "Failed to insert NOTAM into database. Marking database invalid."
            )
            LOGGER.debug("Database write failure", exc_info=exc)
            self._notam_db.set_invalid()
            self._pending_messages.append(message)
            self._schedule_reinitialize("database write failure")

    def _load_queued_messages(self) -> None:
        LOGGER.info("Loading %s queued NOTAMs", len(self._pending_messages))
        while self._pending_messages:
            self._notam_db.put_notam(self._pending_messages.popleft())

    def _on_missed_messages(self, _missed_messages: dict[int, datetime]) -> None:
        self._tracker.clear_only_missed_messages()
        if not self._can_bootstrap_from_fil():
            LOGGER.warning(
                "Missed %s messages but FIL not configured. "
                "Database kept as-is; gaps will remain until next restart with FIL.",
                len(_missed_messages),
            )
            return
        if self._notam_db.is_valid():
            self._notam_db.set_invalid()
            self._schedule_reinitialize("missed message")
        elif self._notam_db.is_initializing():
            self._missed_message_during_initialization = True

    def _on_stale_messages(
        self, last_received_id: int | None, last_received_at: datetime | None
    ) -> None:
        if not self._can_bootstrap_from_fil():
            LOGGER.warning(
                "Message stream is stale but FIL not configured. "
                "last_received_id=%s last_received_at=%s. Database kept as-is.",
                last_received_id,
                last_received_at,
            )
            return
        if self._notam_db.is_valid():
            LOGGER.warning(
                "Message stream is stale. last_received_id=%s last_received_at=%s",
                last_received_id,
                last_received_at,
            )
            self._notam_db.set_invalid()
            self._schedule_reinitialize("stale message stream")

    def _schedule_reinitialize(self, reason: str) -> None:
        LOGGER.info("Scheduling FIL reinitialization reason=%s", reason)
        threading.Thread(
            target=self.initialize_notam_db, name="fil-reinitialize", daemon=True
        ).start()

    def _start_cleanup_loop(self) -> None:
        if not self._config.database.remove_old_notams_enabled:
            return
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, name="cleanup-loop", daemon=True
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        sleep_seconds = (
            max(self._config.database.remove_old_notams_frequency_hours, 1) * 3600
        )
        while not self._stop_event.wait(sleep_seconds):
            try:
                deleted = self._notam_db.remove_old_notams()
                LOGGER.info("Removed %s old NOTAMs", deleted)
            except Exception as exc:
                LOGGER.error("Failed to remove old NOTAMs: %s", exc, exc_info=True)

    def _start_rest_api(self) -> None:
        public_dir = Path(__file__).resolve().parents[1] / "public"
        app = create_rest_app(
            self._notam_db, public_dir=public_dir if public_dir.exists() else None
        )
        config = uvicorn.Config(
            app=app, host="0.0.0.0", port=self._config.rest_api.port, log_level="info"
        )
        self._api_server = uvicorn.Server(config)
        self._api_thread = threading.Thread(
            target=self._api_server.run, name="rest-api", daemon=True
        )
        self._api_thread.start()

    def _iter_replay_files(self, replay_path: Path) -> list[Path]:
        if replay_path.is_dir():
            return sorted(
                path for path in replay_path.iterdir() if path.suffix.lower() == ".xml"
            )
        if replay_path.suffix.lower() != ".xml":
            raise ValueError(
                f"Replay path must point to an XML file or directory: {replay_path}"
            )
        return [replay_path]


def _as_utc_datetime(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime for comparisons."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
