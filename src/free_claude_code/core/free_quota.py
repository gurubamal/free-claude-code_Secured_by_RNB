"""Recognize OpenRouter's daily free quota without echoing upstream prose."""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import time

from free_claude_code.core.diagnostics import attached_upstream_error_body
from free_claude_code.core.failures import ExecutionFailure, FailureKind

MAX_QUOTA_BODY_BYTES = 65536
DAILY_LIMIT_SOURCE = "openrouter_free_tier_daily"


@dataclass(frozen=True)
class DailyFreeQuota:
    reset_at: datetime | None
    limit: int | None
    remaining: int | None

    def message(self) -> str:
        message = "OpenRouter daily free-model request quota exhausted."
        if self.remaining is not None and self.limit is not None:
            message += f" {self.remaining} of {self.limit} daily requests remain."
        if self.reset_at is not None:
            message += (
                f" Provider-reported reset: {self.reset_at:%Y-%m-%d %H:%M:%S UTC}."
            )
        else:
            message += " The provider did not supply a valid reset time."
        return message + (
            " Immediate retries and switching OpenRouter free models cannot restore"
            " this daily allowance. Resume the saved session after the daily reset."
            " Free-only routing remains enabled."
        )

    def failure(self, request_id: str | None = None) -> ExecutionFailure:
        message = self.message()
        if request_id:
            message += f"\n\nRequest ID: {request_id}"
        return ExecutionFailure(FailureKind.RATE_LIMIT, 429, message, False)

    def response_headers(self) -> dict[str, str]:
        headers = {"x-should-retry": "false", "Cache-Control": "no-store"}
        if self.reset_at is not None:
            headers["X-RateLimit-Reset"] = str(int(self.reset_at.timestamp() * 1000))
            headers["Retry-After"] = str(
                max(0, math.ceil(self.reset_at.timestamp() - time()))
            )
        return headers


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 10**15 else None
    if (
        isinstance(value, str)
        and len(value) <= 16
        and value.isascii()
        and value.isdigit()
    ):
        return _nonnegative_int(int(value))
    return None


def _payload(value: object) -> Mapping | None:
    if isinstance(value, bytes | str):
        if len(value) > MAX_QUOTA_BODY_BYTES:
            return None
        try:
            value = json.loads(value)
        except ValueError, UnicodeError, RecursionError:
            return None
    return value if isinstance(value, Mapping) else None


def daily_free_quota(
    body: object, *, status_code: int | None, headers: Mapping | None = None
) -> DailyFreeQuota | None:
    """Accept an explicit daily discriminator, or the legacy daily error marker.

    Never infer daily exhaustion from a generic 429 or from previous_errors.
    HTTP-200 SSE errors can instead carry a structured 429 body code.
    """
    payload = _payload(body)
    if payload is None:
        return None
    error = payload.get("error", payload)
    if not isinstance(error, Mapping):
        return None
    if status_code != 429 and not (
        status_code in {None, 200} and _nonnegative_int(error.get("code")) == 429
    ):
        return None
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    source = metadata.get("limit_source")
    if source != DAILY_LIMIT_SOURCE:
        message = error.get("message")
        if (
            source
            or not isinstance(message, str)
            or "free-models-per-day" not in message.lower()
        ):
            return None
    nested_headers = metadata.get("headers")
    values = {
        str(k).lower(): v
        for mapping in (nested_headers, headers)
        if isinstance(mapping, Mapping)
        for k, v in mapping.items()
    }
    reset_ms = _nonnegative_int(values.get("x-ratelimit-reset"))
    reset_at = None
    # OpenRouter uses Unix epoch milliseconds. Reject malformed/outlandish dates.
    if reset_ms is not None and 946684800000 <= reset_ms <= 32503680000000:
        reset_at = datetime.fromtimestamp(reset_ms / 1000, UTC)
    return DailyFreeQuota(
        reset_at,
        _nonnegative_int(values.get("x-ratelimit-limit")),
        _nonnegative_int(values.get("x-ratelimit-remaining")),
    )


def daily_free_quota_from_error(error: Exception) -> DailyFreeQuota | None:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None)
    bodies = [getattr(error, "body", None), attached_upstream_error_body(error)]
    for body in bodies:
        quota = daily_free_quota(body, status_code=status, headers=headers)
        if quota is not None:
            return quota
    # HTTPStatusError holds the already-read body on its response, not on itself.
    try:
        body = response.content if response is not None else None
    except AttributeError, RuntimeError:
        return None
    return daily_free_quota(body, status_code=status, headers=headers)
