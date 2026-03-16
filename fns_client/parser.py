# FNS NOTAM Client -- A JS Labs Prototype
# Copyright (c) 2026 James Sawyer
# https://labs.jamessawyer.co.uk/ | https://github.com/tg12
#
# PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND. NOT FOR OPERATIONAL
# AVIATION USE. See README.md and LICENSE for full terms.

"""Helpers for splitting FIL XML documents into AIXM messages."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from io import BufferedReader
from typing import BinaryIO


def iter_aixm_messages(stream: BinaryIO) -> Iterator[str]:
    """Yield each AIXMBasicMessage payload from a FIL stream."""

    buffered = stream if isinstance(stream, BufferedReader) else BufferedReader(stream)
    context = ET.iterparse(buffered, events=("end",))
    for _, element in context:
        if element.tag.rsplit("}", 1)[-1] != "AIXMBasicMessage":
            continue
        yield ET.tostring(element, encoding="unicode")
        element.clear()
