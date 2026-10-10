"""Timestamp normalisation shared by the stores and the emr_latest service.

Stored ``created_at`` strings differ in shape between backends (``+00:00`` vs ``Z``, with or without
microseconds).  Ordering and cursors use the parsed UTC instant, and the wire form is one fixed
format, so two backends can never disagree about where a page ends.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_utc(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime (naive input is taken as UTC)."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def norm_ts(value: str | datetime) -> str:
    """Fixed-width UTC form, ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (lexicographic order == time order)."""
    return parse_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
