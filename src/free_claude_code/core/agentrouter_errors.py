"""Fixed AgentRouter diagnostics; never expose raw upstream messages or keys."""

from collections.abc import Mapping

CLIENT_ACCESS_MESSAGE = (
    "AgentRouter rejected this gateway client (unauthorized_client_error). "
    "Ask AgentRouter support to authorize FCC or confirm a supported gateway API "
    "endpoint for your account. This response does not establish that the API "
    "key is invalid. Automatic routing skips this provider while access is rejected."
)


def agentrouter_access_message(payload: object, status_code: int | None) -> str | None:
    if status_code not in (401, 403) or not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    nested = error if isinstance(error, Mapping) else {}
    if (
        payload.get("type") == "unauthorized_client_error"
        or nested.get("type") == "unauthorized_client_error"
        or nested.get("code") == "unauthorized_client_error"
    ):
        return CLIENT_ACCESS_MESSAGE
    # Older responses omit the type. Recognize only this specific phrase;
    # return fixed local prose even if the rest includes secrets or markup.
    message = nested.get("message", payload.get("message"))
    if isinstance(message, str) and message.lower().startswith(
        "unauthorized client detected"
    ):
        return CLIENT_ACCESS_MESSAGE
    return None


class AgentRouterAccessError(Exception):
    def __init__(self):
        self.message = CLIENT_ACCESS_MESSAGE
        super().__init__(self.message)
