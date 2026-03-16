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
