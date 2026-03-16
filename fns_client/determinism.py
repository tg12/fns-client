"""Deterministic runtime helpers for the Python FNS client."""

from __future__ import annotations

import contextlib
import locale
import os
import random
import time


def setup_determinism(seed: int) -> None:
    """Apply deterministic process settings before the service starts."""

    # Seed the standard library RNG even though the current code path avoids randomness.
    random.seed(seed)

    # Export deterministic process defaults for child processes and stable timestamps.
    os.environ["PYTHONHASHSEED"] = str(seed)
    os.environ.setdefault("TZ", "UTC")

    # Normalize locale-dependent formatting so sorting and serialization stay stable.
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_ALL, "C")

    # Apply UTC timezone settings on platforms that support tzset.
    if hasattr(time, "tzset"):
        time.tzset()
