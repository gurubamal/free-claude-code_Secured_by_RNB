"""Daily account exhaustion must terminate, while temporary 429s still retry."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest
from httpx2 import Request, Response
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure
from free_claude_code.core.free_quota import (
    daily_free_quota,
    daily_free_quota_from_error,
)
from free_claude_code.providers.direct_chat import prepare_chat_body
from free_claude_code.providers.open_router import OpenRouterProvider
from tests.providers.request_factory import make_messages_request
from tests.providers.support import (
    SDKStreamDouble,
    immediate_admission,
    make_provider_config,
)

RESET_MS = "1789948800000"


def daily_error():
    return {
        "error": {
            "code": 429,
            "message": "Rate limit exceeded: free-models-per-day",
            "metadata": {
                "limit_source": "openrouter_free_tier_daily",
                "headers": {
                    "X-RateLimit-Limit": "50",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": RESET_MS,
                },
                "previous_errors": [{"message": "DO_NOT_ECHO_UPSTREAM_DATA"}],
            },
        }
    }


@pytest.mark.parametrize("wrapped", [True, False])
def test_daily_message_uses_reset_and_omits_untrusted_history(wrapped, monkeypatch):
    body = daily_error()
    if not wrapped:
        body = body["error"]
    quota = daily_free_quota(body, status_code=429)
    assert quota is not None
    assert "2026-09-21 00:00:00 UTC" in quota.message()
    assert "0 of 50" in quota.message()
    assert "DO_NOT_ECHO" not in quota.message()
    assert quota.failure().retryable is False
    monkeypatch.setattr("free_claude_code.core.free_quota.time", lambda: 1789948740)
    assert quota.response_headers()["Retry-After"] == "60"
    assert quota.response_headers()["x-should-retry"] == "false"


@pytest.mark.parametrize(
    "reset", [None, "tomorrow", "nan", "99999999999999999999", True, "1789948800"]
)
def test_invalid_reset_stays_unknown(reset):
    body = daily_error()
    body["error"]["metadata"]["headers"]["X-RateLimit-Reset"] = reset
    quota = daily_free_quota(body, status_code=429)
    assert quota is not None and quota.reset_at is None
    assert "did not supply a valid reset time" in quota.message()
    assert "Retry-After" not in quota.response_headers()


@pytest.mark.parametrize(
    "body,status",
    [
        ({"error": {"code": 429, "message": "Rate limit exceeded"}}, 429),
        ({"error": {"code": 429, "message": "free-models-per-minute"}}, 429),
        (
            {"error": {"code": 429, "metadata": {"previous_errors": [daily_error()]}}},
            429,
        ),
        (daily_error(), 401),
        ("invalid json", 429),
        ("x" * 65537, 429),
    ],
    ids=["generic", "minute", "history-only", "wrong-status", "malformed", "oversize"],
)
def test_generic_minute_nested_and_malformed_errors_are_not_daily(body, status):
    assert daily_free_quota(body, status_code=status) is None


def test_legacy_daily_marker_and_http_headers():
    quota = daily_free_quota(
        {"message": "Rate limit exceeded: free-models-per-day"},
        status_code=429,
        headers={"x-ratelimit-reset": RESET_MS},
    )
    assert quota is not None and quota.reset_at is not None
    body = daily_error()
    body["error"]["metadata"]["limit_source"] = "some_other_limit"
    assert daily_free_quota(body, status_code=429) is None


def test_structured_sse_and_http_status_error():
    assert daily_free_quota(daily_error(), status_code=200) is not None
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(429, request=request, json=daily_error())
    error = httpx.HTTPStatusError("rejected", request=request, response=response)
    assert daily_free_quota_from_error(error) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("daily,expected_calls", [(True, 1), (False, 5)])
async def test_actual_messages_provider_does_not_retry_daily_quota(
    daily, expected_calls
):
    provider = OpenRouterProvider(
        make_provider_config(
            api_key="synthetic", base_url="https://openrouter.ai/api/v1"
        ),
        admission=immediate_admission(provider_name="OPENROUTER"),
    )
    body = (
        daily_error()
        if daily
        else {"error": {"code": 429, "message": "temporary rate limit"}}
    )
    error = openai.RateLimitError(
        "rate limited",
        response=Response(
            429,
            request=Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        ),
        body=body,
    )
    try:
        with (
            patch.object(
                provider._client.chat.completions,
                "create",
                new=AsyncMock(side_effect=error),
            ) as create,
            pytest.raises(ExecutionFailure) as failure,
        ):
            [
                event
                async for event in provider.stream_messages(
                    make_messages_request("openrouter/free")
                )
            ]
        assert create.await_count == expected_calls
        assert failure.value.status_code == 429
        if daily:
            assert not failure.value.retryable
            assert "2026-09-21 00:00:00 UTC" in failure.value.message
            assert "retry shortly" not in failure.value.message.lower()
            assert "DO_NOT_ECHO" not in failure.value.message
    finally:
        await provider._client.close()


@pytest.mark.parametrize("stream", [False, True])
def test_chat_daily_quota_sends_one_request_and_stops(monkeypatch, stream):
    from free_claude_code.api import free_chat_routes

    requests, responses, closed = [], [], []

    class Upstream:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

        async def send(self, request, **kwargs):
            requests.append(request)
            response = httpx.Response(429, request=request, json=daily_error())
            responses.append(response)
            return response

        async def aclose(self):
            closed.append(True)

    async def unexpected_sleep(*args):
        raise AssertionError("Daily quota exhaustion must not wait/retry")

    monkeypatch.setattr(free_chat_routes.httpx, "AsyncClient", Upstream)
    monkeypatch.setattr(free_chat_routes.asyncio, "sleep", unexpected_sleep)
    settings = Settings(open_router_api_key="synthetic")
    services = SimpleNamespace(
        requests=SimpleNamespace(current_settings=lambda: settings),
        admin=SimpleNamespace(admin_status=None),
        prepare_chat_body=prepare_chat_body,
    )
    from free_helpers import freeze_pool, model

    app = create_app(services)
    freeze_pool(app.state.free_pool, [model("open_router", "openrouter/free")])
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={"messages": [{"role": "user", "content": "hello"}], "stream": stream},
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "free_daily_quota_exhausted"
    assert response.headers["X-RateLimit-Reset"] == RESET_MS
    assert response.headers["x-should-retry"] == "false"
    assert len(requests) == 1
    assert all(r.is_closed for r in responses) and closed
    assert "DO_NOT_ECHO" not in response.text
    assert json.loads(requests[0].content)["model"] == "openrouter/free"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "partial answer"])
async def test_daily_quota_after_stream_acceptance_never_replays(content):
    provider = OpenRouterProvider(
        make_provider_config(
            api_key="synthetic", base_url="https://openrouter.ai/api/v1"
        ),
        admission=immediate_admission(provider_name="OPENROUTER"),
    )
    error = openai.APIError(
        "daily exhaustion",
        request=Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        body=daily_error()["error"],
    )

    async def chunks():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=content, reasoning_content=None, tool_calls=None
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        raise error

    async def create_stream(**kwargs):
        return SDKStreamDouble(chunks())

    events = []
    try:
        with patch.object(
            provider._client.chat.completions,
            "create",
            new=AsyncMock(side_effect=create_stream),
        ) as create:
            try:
                events = [
                    event
                    async for event in provider.stream_messages(
                        make_messages_request("openrouter/free")
                    )
                ]
            except ExecutionFailure as failure:
                events.append(failure.message)
        assert create.await_count == 1
        assert "daily free-model request quota exhausted" in "".join(events)
    finally:
        await provider.cleanup()
