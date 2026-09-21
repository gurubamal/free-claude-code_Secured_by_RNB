"""Bounded stream inspection and provider retry hints for free routing."""

import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .billing_limits import openrouter_billing_failure
from .failures import ExecutionFailure, FailureKind
from .free_quota import daily_free_quota
from .provider_access import plan_access_failure


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
        self.has_output = False

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
                billing = openrouter_billing_failure(self.provider, event, 200)
                if billing:
                    raise billing
                access = plan_access_failure(self.provider, event, 200)
                if access:
                    raise access
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
            self.has_output = self.has_output or _has_output(event)
        if len(self.buffer) > 2 * 1024 * 1024:
            raise ExecutionFailure(
                FailureKind.UPSTREAM,
                502,
                "Provider SSE event exceeded the size limit.",
                False,
            )


def _has_output(event):
    """A role/header/heartbeat is not delivered text, thinking or a tool call."""
    kind = event.get("type", "")
    if not isinstance(kind, str):
        kind = ""
    if kind == "content_block_start":
        block = event.get("content_block") or {}
        return isinstance(block, dict) and bool(
            block.get("text")
            or block.get("thinking")
            or block.get("type") in {"tool_use", "server_tool_use", "redacted_thinking"}
        )
    if kind == "content_block_delta":
        delta = event.get("delta") or {}
        return isinstance(delta, dict) and any(
            delta.get(k) for k in ("text", "thinking", "partial_json", "signature")
        )
    if kind.startswith("response.") and kind.endswith(".delta"):
        return bool(event.get("delta"))
    if kind in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item") or {}
        return isinstance(item, dict) and (
            item.get("type") not in {None, "message", "reasoning"}
            or bool(item.get("content"))
        )
    for choice in event.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if isinstance(delta, dict) and any(
            delta.get(k)
            for k in (
                "content",
                "reasoning",
                "reasoning_content",
                "tool_calls",
                "function_call",
            )
        ):
            return True
    return False
