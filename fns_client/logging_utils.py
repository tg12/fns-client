# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Logging helpers for the Python FNS client."""

from __future__ import annotations

import logging
from typing import Final

try:
    import colorlog
except ImportError:  # pragma: no cover
    colorlog = None


DEFAULT_LOG_FORMAT: Final[str] = (
    "%(log_color)sts=%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s%(reset)s"
)


def _resolve_log_level(level: str) -> int:
    """Resolve a user-provided log level name into a logging constant."""

    return getattr(logging, level.upper(), logging.INFO)


def configure_logging(level: str = "INFO") -> None:
    """Configure process-wide logging."""

    # Build one stream handler so all logs share a single structured format.
    if colorlog is not None:
        handler = colorlog.StreamHandler()
        handler.setFormatter(colorlog.ColoredFormatter(DEFAULT_LOG_FORMAT))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "ts=%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s"
            )
        )

    # Reset root handlers so repeat startups do not duplicate log lines.
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(_resolve_log_level(level))
    root_logger.addHandler(handler)
