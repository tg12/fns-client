# ARCHIVED — project superseded by phantom-tide/ (fns-client still required as NOTAM data source)
# This file is kept for reference only. Do not edit.
# Migration date: 2026-03-19

# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""CLI entry point for the Python FNS client."""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from fns_client.config import load_config
from fns_client.determinism import setup_determinism
from fns_client.logging_utils import configure_logging
from fns_client.service import FnsClientService

LOGGER = logging.getLogger(__name__)


# Keep runtime bootstrap variables at the top of the file so startup
# behavior is explicit.
CONFIG_PATH = Path("src/main/resources/fnsClient.conf")


def _resolve_replay_path(config_path: Path, replay_path: str) -> Path:
    """Resolve the replay path relative to the configuration file location."""

    # Resolve replay data relative to the repository root when a relative path is used.
    candidate = Path(replay_path)
    if candidate.is_absolute():
        return candidate
    return (config_path.parent.parent.parent / candidate).resolve()


def main() -> int:
    """Run the configured service mode."""

    # Load the only supported configuration source.
    config = load_config(CONFIG_PATH)

    # Apply deterministic process settings before any worker threads start.
    setup_determinism(config.runtime.deterministic_seed)

    # Configure structured logging after the configuration file is available.
    configure_logging(config.log_level)

    # Derive the replay source from the configuration file when replay mode is active.
    replay_path = None
    if config.runtime.mode.lower() == "replay":
        replay_path = _resolve_replay_path(CONFIG_PATH, config.runtime.replay_path)

    service = FnsClientService(config, replay_path=replay_path)

    # Allow config-driven validation without CLI switches.
    if config.runtime.validate_on_startup:
        return 0 if service.validate_notam_db() else 1

    # Install shutdown handlers before starting background threads.
    _install_signal_handlers(service)
    service.start()
    try:
        service.wait_forever()
    finally:
        service.stop()
    return 0


def _install_signal_handlers(service: FnsClientService) -> None:
    """Attach SIGINT and SIGTERM handlers to the running service."""

    def _handle_signal(signum: int, _frame) -> None:
        # Stop the service cleanly before exiting the process.
        LOGGER.info("Received signal=%s", signum)
        service.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    sys.exit(main())
