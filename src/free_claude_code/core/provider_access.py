"""Recognize explicit account-wide API access denial without copying secrets."""

import json
from collections.abc import Mapping

from .failures import ExecutionFailure, FailureKind


def plan_access_failure(provider: str, body: object, status_code: int | None):
    """Generic 403s stay model-specific; only reviewed error codes block a provider."""
    if provider.lower() != "commandcode" or status_code not in {200, 403}:
        return None
    if isinstance(body, str | bytes | bytearray):
        if len(body) > 65536:
            return None
        try:
            body = json.loads(body)
        except ValueError, UnicodeError:
            return None
    if not isinstance(body, Mapping):
        return None
    # The OpenAI SDK unwraps the error object; raw HTTP/SSE retains the envelope.
    error = body.get("error", body)
    if not isinstance(error, Mapping) or error.get("code") != "upgrade_required":
        return None
    return ExecutionFailure(
        FailureKind.PERMISSION,
        403,
        "CommandCode's current plan does not include API access. This provider is "
        "skipped while automatic routing tries other enabled providers. Configure "
        "an API-enabled account in Admin to use CommandCode.",
        False,
        provider_access_blocked=True,
    )
