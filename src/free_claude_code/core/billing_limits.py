"""Recognize OpenRouter billing scope without retaining account URLs or history."""

import json
import re
from collections.abc import Mapping
from contextlib import suppress

from .diagnostics import attached_upstream_error_body
from .failures import BillingLimit, ExecutionFailure, FailureKind

_SOURCES = {
    "openrouter_credits": BillingLimit.REQUEST_BUDGET,
    "openrouter_key_limit": BillingLimit.KEY_LIMIT,
    "openrouter_in_flight_budget": BillingLimit.IN_FLIGHT_BUDGET,
}
_LEGACY_REQUEST_BUDGET = re.compile(
    r"^(?:(?:prompt tokens|max_tokens) limit exceeded:\s*\d+\s*>\s*\d+"
    r"|this request requires more credits, or fewer max_tokens)",
    re.IGNORECASE,
)
_MESSAGES = {
    BillingLimit.REQUEST_BUDGET: (
        "OpenRouter cannot fund this request at the selected model's price. "
        "Automatic routing can try other eligible providers and models; "
        "this does not prove that the account balance is exhausted."
    ),
    BillingLimit.KEY_LIMIT: (
        "OpenRouter's API key spending limit was reached. Automatic routing "
        "can try other enabled providers without changing the spending limit."
    ),
    BillingLimit.IN_FLIGHT_BUDGET: (
        "OpenRouter's in-flight spending budget is temporarily occupied. "
        "Automatic routing can try other enabled providers while it settles."
    ),
    None: (
        "OpenRouter rejected this request for billing reasons. Automatic routing "
        "can try other enabled providers. Check billing and key limits in Admin."
    ),
}


def openrouter_billing_failure(provider, body, status_code):
    """Read only the current error, never previous_errors or remedy prose.

    openrouter_credits can mean a positive balance cannot fund one large request.
    It is not evidence that cheaper sibling models are unavailable. Unknown
    billing errors remain account-scoped, with sanitized diagnostic wording.
    """
    if provider.lower() not in {"open_router", "openrouter"}:
        return None
    if isinstance(body, str | bytes | bytearray):
        if len(body) <= 65536:
            try:
                body = json.loads(body)
            except ValueError, UnicodeError, RecursionError:
                body = None
        else:
            body = None
    error = body.get("error", body) if isinstance(body, Mapping) else {}
    error = error if isinstance(error, Mapping) else {}
    if status_code != 402 and not (
        status_code in {None, 200}
        and (error.get("code") == 402 or error.get("code") == "402")
    ):
        return None
    metadata = error.get("metadata")
    source = metadata.get("limit_source") if isinstance(metadata, Mapping) else None
    limit = _SOURCES.get(source) if isinstance(source, str) else None
    message = error.get("message")
    if (
        source is None
        and isinstance(message, str)
        and _LEGACY_REQUEST_BUDGET.match(message[:4096])
    ):
        limit = BillingLimit.REQUEST_BUDGET
    return ExecutionFailure(
        FailureKind.PERMISSION, 402, _MESSAGES[limit], False, billing_limit=limit
    )


def openrouter_billing_from_error(provider, error):
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(response, "status_code", None)
    bodies = [getattr(error, "body", None), attached_upstream_error_body(error)]
    with suppress(AttributeError, RuntimeError):
        bodies.append(response.content if response is not None else None)
    generic = None
    for body in bodies:
        failure = openrouter_billing_failure(provider, body, status)
        if failure is not None:
            if failure.billing_limit is not None:
                return failure
            generic = failure
    return generic
