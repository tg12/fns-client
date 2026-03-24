# ARCHIVED — configuration migrated to phantom-tide/core/constants.py
# This file is kept for reference only. Do not edit.
# Migration date: 2026-03-19

# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Configuration loading for the Python FNS client."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _strip_inline_comment(raw_line: str) -> str:
    in_quotes = False
    escaped = False
    result: list[str] = []
    for char in raw_line:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            result.append(char)
            continue
        if char == "#" and not in_quotes:
            break
        result.append(char)
    return "".join(result).strip()


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    lower_value = value.lower()
    if lower_value == "true":
        return True
    if lower_value == "false":
        return False
    if lower_value == "null":
        return None
    if value.startswith("[") or value.startswith("{"):
        return value
    try:
        return int(value)
    except ValueError:
        return value


def _nested_get(mapping: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = mapping
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class FilConfig:
    """Configuration for the FIL SFTP client."""

    host: str = ""
    port: int = 22
    username: str = ""
    strict_host_key_checking: str = "no"
    known_hosts_file_path: str = ""
    cert_file_path: str = ""
    fil_file_save_path: str = ""
    fil_date_file_name: str = "primary_last_date.txt"
    fil_data_file_time_format: str = "%Y-%m-%d %H:%M:%S %Z"
    fil_file_name: str = "initial_load_aixm.xml.gz"


@dataclass(frozen=True)
class JmsConfig:
    """Configuration for the Solace-backed message consumer."""

    initial_context_factory: str = ""
    provider_url: str = ""
    username: str = ""
    password: str = ""
    solace_message_vpn: str = ""
    solace_ssl_trust_store: str = ""
    solace_validate_certificate: bool = True
    solace_jndi_connection_retries: int = -1
    connection_factory: str = ""
    destination: str = ""
    message_processing_threads: int = 4
    enabled: bool = True


@dataclass(frozen=True)
class DatabaseConfig:
    """Configuration for NOTAM persistence."""

    driver: str = "org.h2.Driver"
    connection_url: str = "jdbc:h2:./Notams;mode=MySQL;AUTO_SERVER=TRUE"
    username: str = ""
    password: str = ""
    schema: str = "PUBLIC"
    table: str = "NOTAMS"
    initialization_retry_count: int = 3
    remove_old_notams_enabled: bool = True
    remove_old_notams_frequency_hours: int = 24


@dataclass(frozen=True)
class MessageTrackerConfig:
    """Configuration for missed/stale message tracking."""

    schedule_rate_seconds: int = 10
    missed_message_trigger_time_minutes: int = 5
    stale_message_trigger_time_minutes: int = 10


@dataclass(frozen=True)
class RestApiConfig:
    """Configuration for the REST API."""

    enabled: bool = True
    port: int = 8080


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime control values loaded from the configuration file."""

    mode: str = "replay"
    replay_path: str = "docker/replay-data"
    deterministic_seed: int = 7
    validate_on_startup: bool = False


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""

    fil: FilConfig = field(default_factory=FilConfig)
    jms: JmsConfig = field(default_factory=JmsConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    message_tracker: MessageTrackerConfig = field(default_factory=MessageTrackerConfig)
    rest_api: RestApiConfig = field(default_factory=RestApiConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    log_level: str = "INFO"


def parse_typesafe_style_config(content: str) -> dict[str, Any]:
    """Parse the simple key=value format used by the legacy project."""

    result: dict[str, Any] = {}
    for raw_line in content.splitlines():
        line = _strip_inline_comment(raw_line)
        if not line or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        current = result
        key_parts = [part.strip() for part in key.strip().split(".") if part.strip()]
        if not key_parts:
            continue
        for part in key_parts[:-1]:
            current = current.setdefault(part, {})
        current[key_parts[-1]] = _parse_scalar(raw_value)
    return result


def build_app_config(mapping: dict[str, Any]) -> AppConfig:
    """Convert a parsed configuration mapping into typed dataclasses."""

    # Build each configuration section explicitly so the operator-facing file
    # stays stable.
    return AppConfig(
        fil=FilConfig(
            host=str(_nested_get(mapping, "fil.sftp.host", "") or ""),
            port=int(_nested_get(mapping, "fil.sftp.port", 22) or 22),
            username=str(_nested_get(mapping, "fil.sftp.username", "") or ""),
            strict_host_key_checking=str(
                _nested_get(mapping, "fil.sftp.strictHostKeyChecking", "no") or "no"
            ),
            known_hosts_file_path=str(
                _nested_get(mapping, "fil.sftp.knownHostsFilePath", "") or ""
            ),
            cert_file_path=str(_nested_get(mapping, "fil.sftp.certFilePath", "") or ""),
            fil_file_save_path=str(
                _nested_get(mapping, "fil.sftp.filFileSavePath", "") or ""
            ),
        ),
        jms=JmsConfig(
            initial_context_factory=str(
                _nested_get(mapping, "jms.initialContextFactory", "") or ""
            ),
            provider_url=str(_nested_get(mapping, "jms.providerUrl", "") or ""),
            username=str(_nested_get(mapping, "jms.username", "") or ""),
            password=str(_nested_get(mapping, "jms.password", "") or ""),
            solace_message_vpn=str(
                _nested_get(mapping, "jms.solace.messageVpn", "") or ""
            ),
            solace_ssl_trust_store=str(
                _nested_get(mapping, "jms.solace.sslTrustStore", "") or ""
            ),
            solace_validate_certificate=_to_bool(
                _nested_get(mapping, "jms.solace.validateCertificate", True), True
            ),
            solace_jndi_connection_retries=int(
                _nested_get(mapping, "jms.solace.jndiConnectionRetries", -1) or -1
            ),
            connection_factory=str(
                _nested_get(mapping, "jms.connectionFactory", "") or ""
            ),
            destination=str(_nested_get(mapping, "jms.destination", "") or ""),
            message_processing_threads=int(
                _nested_get(mapping, "jms.messageProcessor.processingThreads", 4) or 4
            ),
            enabled=_to_bool(_nested_get(mapping, "jms.enabled", True), True),
        ),
        database=DatabaseConfig(
            driver=str(
                _nested_get(mapping, "notamDb.driver", "org.h2.Driver")
                or "org.h2.Driver"
            ),
            connection_url=str(
                _nested_get(
                    mapping,
                    "notamDb.connectionUrl",
                    "jdbc:h2:./Notams;mode=MySQL;AUTO_SERVER=TRUE",
                )
                or "jdbc:h2:./Notams;mode=MySQL;AUTO_SERVER=TRUE"
            ),
            username=str(_nested_get(mapping, "notamDb.username", "") or ""),
            password=str(_nested_get(mapping, "notamDb.password", "") or ""),
            schema=str(_nested_get(mapping, "notamDb.schema", "PUBLIC") or "PUBLIC"),
            table=str(_nested_get(mapping, "notamDb.table", "NOTAMS") or "NOTAMS"),
            initialization_retry_count=int(
                _nested_get(mapping, "notamDb.initializationRetryCount", 3) or 3
            ),
            remove_old_notams_enabled=_to_bool(
                _nested_get(mapping, "notamDb.removeOldNotams.enabled", True),
                True,
            ),
            remove_old_notams_frequency_hours=int(
                _nested_get(mapping, "notamDb.removeOldNotams.frequency", 24) or 24
            ),
        ),
        message_tracker=MessageTrackerConfig(
            schedule_rate_seconds=int(
                _nested_get(mapping, "messageTracker.scheduleRate", 10) or 10
            ),
            missed_message_trigger_time_minutes=int(
                _nested_get(mapping, "messageTracker.missedMessageTriggerTime", 5) or 5
            ),
            stale_message_trigger_time_minutes=int(
                _nested_get(mapping, "messageTracker.staleMessageTriggerTime", 10) or 10
            ),
        ),
        rest_api=RestApiConfig(
            enabled=_to_bool(_nested_get(mapping, "restapi.enabled", True), True),
            port=int(_nested_get(mapping, "restapi.port", 8080) or 8080),
        ),
        runtime=RuntimeConfig(
            mode=str(_nested_get(mapping, "runtime.mode", "replay") or "replay"),
            replay_path=str(
                _nested_get(mapping, "runtime.replayPath", "docker/replay-data")
                or "docker/replay-data"
            ),
            deterministic_seed=int(
                _nested_get(mapping, "runtime.deterministicSeed", 7) or 7
            ),
            validate_on_startup=_to_bool(
                _nested_get(mapping, "runtime.validateOnStartup", False),
                False,
            ),
        ),
        log_level=str(_nested_get(mapping, "logging.level", "INFO") or "INFO"),
    )


def load_config(path: Path) -> AppConfig:
    """Load the application configuration from disk."""

    return build_app_config(
        parse_typesafe_style_config(path.read_text(encoding="utf-8"))
    )
