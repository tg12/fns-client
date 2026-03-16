# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Message source implementations for live and replay operation."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fns_client.config import JmsConfig

LOGGER = logging.getLogger(__name__)

CORRELATION_ID_PROPERTY = "us_gov_dot_faa_aim_fns_nds_CorrelationID"
STATUS_PROPERTY = "us_gov_dot_faa_aim_fns_nds_NOTAMStatus"


@dataclass(frozen=True)
class MessageEnvelope:
    """Transport-neutral representation of one incoming message."""

    correlation_id: int
    status: str
    payload: str
    broker_timestamp: datetime


class MessageSource:
    """Interface for sources that emit FNS JMS-style messages."""

    def iter_messages(self) -> Iterator[MessageEnvelope]:
        """Yield messages indefinitely until the caller stops consuming."""

        raise NotImplementedError

    def close(self) -> None:
        """Release source resources."""


class ReplayFileMessageSource(MessageSource):
    """Replay one XML file or one directory of XML files as ACTIVE messages."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def iter_messages(self) -> Iterator[MessageEnvelope]:
        files = [self._path]
        if self._path.is_dir():
            files = sorted(
                path for path in self._path.iterdir() if path.suffix.lower() == ".xml"
            )
        correlation_id = 1
        for file_path in files:
            yield MessageEnvelope(
                correlation_id=correlation_id,
                status="ACTIVE",
                payload=file_path.read_text(encoding="utf-8"),
                broker_timestamp=datetime.now(UTC),
            )
            correlation_id += 1


class SolaceMessageSource(MessageSource):
    """Consume messages from a Solace queue using the Python PubSub+ client."""

    def __init__(self, config: JmsConfig) -> None:
        self._config = config
        self._messaging_service = None
        self._receiver = None

    def iter_messages(self) -> Iterator[MessageEnvelope]:
        properties = self._build_properties()
        try:
            from solace.messaging.messaging_service import MessagingService
            from solace.messaging.resources.queue import Queue
        except ImportError as exc:
            raise RuntimeError(
                "solace-pubsubplus is required for live JMS consumption"
            ) from exc

        while True:
            try:
                if self._messaging_service is None:
                    self._messaging_service = (
                        MessagingService.builder().from_properties(properties).build()
                    )
                    self._messaging_service.connect()
                    queue = Queue.durable_exclusive_queue(self._config.destination)
                    builder = (
                        self._messaging_service.create_persistent_message_receiver_builder()
                    )
                    self._receiver = builder.build(queue)
                    self._receiver.start()
                    LOGGER.info(
                        "Connected live message source queue=%s",
                        self._config.destination,
                    )

                assert self._receiver is not None
                message = self._receiver.receive_message(timeout=5000)
                if message is None:
                    continue
                envelope = MessageEnvelope(
                    correlation_id=_coerce_int(
                        _extract_property(message, CORRELATION_ID_PROPERTY), -1
                    ),
                    status=_extract_property(message, STATUS_PROPERTY) or "ACTIVE",
                    payload=_extract_payload(message),
                    broker_timestamp=datetime.now(UTC),
                )
                _ack_message(self._receiver, message)
                yield envelope
            except Exception as exc:
                LOGGER.error("Live message consumption failed: %s", exc, exc_info=True)
                self.close()
                time.sleep(5)

    def close(self) -> None:
        if self._receiver is not None:
            try:
                self._receiver.terminate()
            except Exception:
                LOGGER.debug("Ignoring receiver shutdown failure", exc_info=True)
            self._receiver = None
        if self._messaging_service is not None:
            try:
                self._messaging_service.disconnect()
            except Exception:
                LOGGER.debug(
                    "Ignoring messaging service shutdown failure", exc_info=True
                )
            self._messaging_service = None

    def _build_properties(self) -> dict[str, str]:
        host = _provider_url_to_host(self._config.provider_url)
        properties = {
            "solace.messaging.transport.host": host,
            "solace.messaging.service.vpn-name": self._config.solace_message_vpn,
            "solace.messaging.authentication.basic.username": self._config.username,
            "solace.messaging.authentication.basic.password": self._config.password,
        }
        if self._config.solace_ssl_trust_store:
            properties["solace.messaging.tls.trust-store-path"] = (
                self._config.solace_ssl_trust_store
            )
        if not self._config.solace_validate_certificate:
            properties["solace.messaging.tls.cert-validated"] = False
        return properties


def _provider_url_to_host(provider_url: str) -> str:
    if not provider_url or provider_url == "JMS_PROVIDER_URL":
        raise ValueError("jms.providerUrl must be configured for live consumption")
    parsed = urlparse(provider_url)
    if parsed.scheme and parsed.netloc:
        return provider_url
    return f"tcps://{provider_url}"


def _extract_property(message: object, name: str) -> str | None:
    getter_names = ("get_property", "getProperty", "get_application_message_property")
    for getter_name in getter_names:
        getter = getattr(message, getter_name, None)
        if getter is None:
            continue
        try:
            value = getter(name)
        except Exception:
            continue
        if value is not None:
            return str(value)
    return None


def _extract_payload(message: object) -> str:
    for getter_name in ("get_payload_as_string", "get_payload_as_bytes"):
        getter = getattr(message, getter_name, None)
        if getter is None:
            continue
        payload = getter()
        if isinstance(payload, bytes):
            return payload.decode("utf-8")
        return str(payload)
    payload = getattr(message, "payload", None)
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    if payload is not None:
        return str(payload)
    raise ValueError("Unable to extract payload from broker message")


def _ack_message(receiver: object, message: object) -> None:
    for method_name in ("ack", "acknowledge"):
        method = getattr(receiver, method_name, None)
        if method is None:
            continue
        try:
            method(message)
            return
        except Exception:
            LOGGER.debug("Ack method %s failed", method_name, exc_info=True)


def _coerce_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
