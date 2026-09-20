"""Offline wire tests for max effort, client overrides and fallback isolation."""

import json
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from free_helpers import freeze_pool
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.api.free_chat_routes import chat_target
from free_claude_code.api.handlers import MessagesHandler
from free_claude_code.application.free_pool import FreeModel
from free_claude_code.application.routing import ModelRouter
from free_claude_code.cli.launchers.claude import _configure
from free_claude_code.cli.launchers.runner import LaunchContext
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic.models import MessagesRequest
from free_claude_code.core.openai_responses import OpenAIResponsesRequest
from free_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from free_claude_code.harnesses.resources import LaunchResources
from free_claude_code.providers.direct_chat import prepare_chat_body
from tests.api.test_api_handlers import (
    _CLASSIFIER_USER,
    _CURRENT_CLASSIFIER_SYSTEM,
    FakeProvider,
    _streaming_body_text,
)
from tests.web_tools_support import StubWebToolsClient


@pytest.mark.parametrize(
    "provider,field,expected",
    [
        ("inception", "reasoning_effort", "high"),
        ("gemini", "reasoning_effort", "high"),
        ("gemini_oauth", "reasoning_effort", "high"),
        ("deepseek", "reasoning_effort", "max"),
        ("open_router", "reasoning", {"effort": "max"}),
        ("kilo", "reasoning", {"effort": "max"}),
        ("mistral", "reasoning_effort", "high"),
        ("lmstudio", "reasoning_effort", "high"),
        (
            "nvidia_nim",
            "chat_template_kwargs",
            {"thinking": True, "enable_thinking": True, "reasoning_budget": 8192},
        ),
    ],
)
@pytest.mark.parametrize("client_effort", ["low", "none", "max"])
def test_max_effort_reaches_provider_wire(provider, field, expected, client_effort):
    payload = {
        "model": "synthetic",
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": "test"}],
        "reasoning_effort": client_effort,
        "extra_body": {"reasoning": {"enabled": False}, "plugins": ["paid"]},
    }
    original = deepcopy(payload)
    result = prepare_chat_body(Settings(reasoning_policy="max"), provider, payload)
    assert result[field] == expected
    assert "extra_body" not in result and "plugins" not in result
    assert payload == original


@pytest.mark.parametrize("provider", ["atria", "cline_pass", "commandcode", "kimchi"])
def test_unsupported_effort_is_not_invented(provider):
    result = prepare_chat_body(
        Settings(reasoning_policy="max"),
        provider,
        {"model": "synthetic", "reasoning_effort": "max"},
    )
    assert "reasoning_effort" not in result and "reasoning" not in result


def test_inception_wire_and_history_are_isolated_from_next_fallback():
    payload = {
        "messages": [{"role": "user", "content": "hello", "name": "author"}],
        "max_completion_tokens": 32000,
        "parallel_tool_calls": True,
        "top_p": 0.8,
    }
    before = deepcopy(payload)
    model = FreeModel(
        "inception", "mercury-2.5", 260000, 65536, True, False, "paid_api"
    )
    _, _, body = chat_target(
        Settings(reasoning_policy="max"), model, payload, prepare_body=prepare_chat_body
    )
    assert body["max_completion_tokens"] == 8192 and "max_tokens" not in body
    assert body["reasoning_effort"] == "high"
    assert "top_p" not in body and "parallel_tool_calls" not in body
    assert "name" not in body["messages"][0]
    assert payload == before


@pytest.mark.parametrize("stream", [False, True])
def test_max_survives_real_chat_ingress_fallback(monkeypatch, stream):
    from free_claude_code.api import free_chat_routes

    settings = Settings(
        reasoning_policy="max",
        allow_paid_api_models=True,
        routing_category_order="free,paid_api,subscription",
        routing_provider_priority="open_router,gemini,deepseek",
        open_router_api_key="synthetic",
        gemini_api_key="synthetic",
        deepseek_api_key="synthetic",
    )
    services = SimpleNamespace(
        requests=SimpleNamespace(current_settings=lambda: settings),
        admin=SimpleNamespace(admin_status=None),
        prepare_chat_body=prepare_chat_body,
    )
    app = create_app(services)
    freeze_pool(
        app.state.free_pool,
        [
            FreeModel(
                "open_router", "synthetic:free", 1048576, 8192, True, False, "free"
            ),
            FreeModel("gemini", "synthetic", 1048576, 8192, True, False, "free"),
            FreeModel("deepseek", "synthetic", 1048576, 8192, True, False, "paid_api"),
        ],
    )
    bodies = []

    def serve(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert body["reasoning"] == {"effort": "max"}
            assert all(x == 0 for x in body["provider"]["max_price"].values())
            return httpx.Response(429, json={"error": "synthetic quota"})
        if len(bodies) == 2:
            assert body["reasoning_effort"] == "high"
            assert "provider" not in body and "reasoning" not in body
            return httpx.Response(503, json={"error": "synthetic outage"})
        assert body["reasoning_effort"] == "max"
        assert "provider" not in body and "reasoning" not in body
        if stream:
            return httpx.Response(
                200,
                content='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        free_chat_routes.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(serve), **kwargs),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={
                "messages": [{"role": "user", "content": "test"}],
                "reasoning_effort": "none",
                "stream": stream,
                "extra_body": {"reasoning": {"enabled": False}},
            },
        )
    assert response.status_code == 200 and "OK" in response.text
    assert response.headers["X-FCC-Provider"] == "deepseek"
    assert len(bodies) == 3


@pytest.mark.parametrize("automatic", [False, True])
def test_messages_and_responses_max_overrides_client_disable(automatic):
    settings = Settings(
        auto_free_models=automatic,
        reasoning_policy="max",
        reasoning_fable="max",
        reasoning_opus="max",
        reasoning_sonnet="max",
        reasoning_haiku="max",
    )
    router = ModelRouter(settings)
    for name in [
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4.5",
        "claude-3-freecc-no-thinking/deepseek/synthetic",
    ]:
        message = MessagesRequest(
            model=name,
            max_tokens=8192,
            messages=[{"role": "user", "content": "test"}],
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
        )
        response = OpenAIResponsesRequest(
            model=name, input="test", reasoning={"effort": "none"}
        )
        for routed in (
            router.resolve_messages_request(message),
            router.resolve_responses_request(response),
        ):
            assert routed.reasoning == ReasoningPolicy.on(effort=ReasoningEffort.MAX)


@pytest.mark.asyncio
async def test_classifier_retains_max_and_verdict_filter():
    provider = FakeProvider()
    handler = MessagesHandler(
        Settings(reasoning_policy="max"),
        provider_resolver=AsyncMock(return_value=provider),
        web_tools=StubWebToolsClient(),
    )
    request = MessagesRequest(
        model="open_router/synthetic",
        max_tokens=8192,
        stream=True,
        system=_CURRENT_CLASSIFIER_SYSTEM,
        stop_sequences=["</severity>"],
        messages=[{"role": "user", "content": _CLASSIFIER_USER}],
    )
    from unittest.mock import patch

    from free_claude_code.api.handlers.messages import classifier_response

    with patch(
        "free_claude_code.api.handlers.messages.classifier_response",
        wraps=classifier_response,
    ) as filtered:
        await _streaming_body_text(await handler.create(request))
    assert provider.stream_kwargs[0]["reasoning"] == ReasoningPolicy.on(
        effort=ReasoningEffort.MAX
    )
    assert provider.requests[0].stop_sequences is None
    filtered.assert_called_once()


def test_claude_launch_inherits_max_without_weakening_permissions():
    settings = Settings(reasoning_policy="max")
    ctx = LaunchContext(
        "claude",
        settings,
        "http://127.0.0.1:8082",
        "synthetic",
        {"CLAUDE_CODE_EFFORT_LEVEL": "low"},
        None,
    )
    with ExitStack() as stack:
        launch = _configure(ctx, ["-p", "test"], LaunchResources(stack))
    assert launch.env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
    assert "--dangerously-skip-permissions" not in launch.command
    assert launch.command[launch.command.index("--permission-mode") + 1] == "default"
