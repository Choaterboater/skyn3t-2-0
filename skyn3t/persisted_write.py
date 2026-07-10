"""Stable persisted-write receipt format shared by codegen and final proof."""

from __future__ import annotations

import json
import re
from typing import Any

PERSISTED_WRITE_RECEIPT_KEY = "__skyn3t_persisted_write_receipt__"
PERSISTED_WRITE_RECEIPT_MAX_BYTES = 2048
_LEGACY_RECEIPT_RE = re.compile(
    r"\[persisted to workspace:\s*\d+\s+UTF-8 bytes;\s*"
    r"use read_file to inspect\]",
    re.IGNORECASE,
)


def is_persisted_write_receipt_body(value: Any) -> bool:
    """Return true only for an exact, small legacy or versioned receipt body."""
    if not isinstance(value, str):
        return False
    if len(value.encode("utf-8", "replace")) > PERSISTED_WRITE_RECEIPT_MAX_BYTES:
        return False
    content = value.strip().lstrip("\ufeff")
    if _LEGACY_RECEIPT_RE.fullmatch(content):
        return True
    if not (content.startswith("{") and content.endswith("}")):
        return False
    try:
        metadata = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(metadata, dict) and PERSISTED_WRITE_RECEIPT_KEY in metadata


def is_persisted_write_receipt(value: Any) -> bool:
    """Return true when a write argument/item is history metadata, not content."""
    if not isinstance(value, dict):
        return False
    if PERSISTED_WRITE_RECEIPT_KEY in value:
        return True
    content = value.get("content")
    if isinstance(content, dict):
        return PERSISTED_WRITE_RECEIPT_KEY in content
    return is_persisted_write_receipt_body(content)
