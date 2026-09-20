"""Bounded stream inspection and provider retry hints for free routing."""

import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .failures import ExecutionFailure, FailureKind
from .free_quota import daily_free_quota


def retry_seconds(headers):
    value = headers.get("retry-after") if headers else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except TypeError, ValueError:
        try:
            seconds = (
                parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)
            ).total_seconds()
        except TypeError, ValueError, OverflowError:
            return None
    return min(172800, max(0, seconds)) if math.isfinite(seconds) else None


class FreeStreamCheck:
    """Observe SSE without changing it; never persist upstream content."""

    def __init__(self, provider):
        self.provider = provider
        self.buffer = b""
        self.complete = False

    def feed(self, chunk):
        self.buffer += chunk.encode() if isinstance(chunk, str) else chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            value = line[5:].strip()
            if value == b"[DONE]":
                self.complete = True
                continue
            try:
                event = json.loads(value)
            except ValueError, UnicodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") in {
                "message_stop",
                "response.completed",
                "response.incomplete",
            }:
                self.complete = True
            if event.get("error") or event.get("type") in {"error", "response.failed"}:
                quota = (
                    daily_free_quota(event, status_code=200)
                    if self.provider == "open_router"
                    else None
                )
                if quota:
                    raise quota.failure()
                raise ExecutionFailure(
                    FailureKind.UPSTREAM,
                    502,
                    f"Free provider {self.provider} returned a stream error.",
                    False,
                )
        if len(self.buffer) > 2 * 1024 * 1024:
            raise ExecutionFailure(
                FailureKind.UPSTREAM,
                502,
                "Provider SSE event exceeded the size limit.",
                False,
            )
