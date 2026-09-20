"""Allowlisted Google access diagnostics without account IDs, keys or raw messages."""

from collections.abc import Mapping

_MESSAGES = {
    "CONSUMER_SUSPENDED": (
        "Google denied access: the Cloud project for this key is suspended "
        "(CONSUMER_SUSPENDED). Review the project's suspension notice in Google "
        "Cloud and contact Google Support or appeal. Retrying or changing a key "
        "on the same project will not restore access."
    ),
    "SERVICE_DISABLED": (
        "The Gemini API is disabled for this Google Cloud project "
        "(SERVICE_DISABLED). Enable the Generative Language API in Google Cloud."
    ),
    "API_KEY_INVALID": (
        "Google rejected this API key (API_KEY_INVALID). Save a valid Gemini API "
        "key from Google AI Studio in Admin."
    ),
}


def google_access_message(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping) or not isinstance(error.get("details"), list):
        return None
    for detail in error["details"]:
        if not isinstance(detail, Mapping) or detail.get("domain") != "googleapis.com":
            continue
        reason = detail.get("reason")
        if isinstance(reason, str) and reason in _MESSAGES:
            return _MESSAGES[reason]
    return None


class GoogleAccessError(Exception):
    """Only a fixed message selected from a Google ErrorInfo code may be exposed."""

    def __init__(self, message: str):
        if message not in _MESSAGES.values():
            raise ValueError("Unknown Google access diagnostic")
        self.message = message
        super().__init__(message)
