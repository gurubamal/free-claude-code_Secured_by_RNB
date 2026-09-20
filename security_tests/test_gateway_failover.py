"""Account denial and pre-output gateway failure must reach other providers."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import httpx2
import openai
import pytest
from free_helpers import freeze_pool
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.application.execution import ProviderExecutor
from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.application.routing import ModelRouter
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic import MessagesRequest
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.free_stream import FreeStreamCheck
from free_claude_code.core.openai_responses import OpenAIResponsesRequest
from free_claude_code.core.provider_access import plan_access_failure
from free_claude_code.providers.direct_chat import prepare_chat_body
from free_claude_code.providers.failure_policy import classify_provider_failure


def model(provider, name, billing="paid_api"):
    return FreeModel(provider, name, 1048576, 8192, True, False, billing)


def denial():
    return {
        "error": {"code": "upgrade_required", "message": "DO_NOT_ECHO_PRIVATE_DETAIL"}
    }


@pytest.mark.parametrize("unwrapped", [False, True])
def test_explicit_plan_denial_skips_siblings_and_survives_restart(unwrapped):
    error = openai.PermissionDeniedError(
        "synthetic",
        response=httpx2.Response(
            403,
            request=httpx2.Request(
                "POST", "https://api.commandcode.ai/provider/v1/chat/completions"
            ),
        ),
        body=denial()["error"] if unwrapped else denial(),
    )
    failure = classify_provider_failure(
        error, provider_name="COMMANDCODE", read_timeout_s=45, request_id="synthetic"
    )
    assert failure.provider_access_blocked and failure.status_code == 403
    assert "DO_NOT_ECHO" not in failure.message
    settings = Settings(commandcode_api_key="synthetic", allow_paid_api_models=True)
    first, sibling = model("commandcode", "one"), model("commandcode", "two")
    pool = AutomaticFreePool()
    pool.record_failure(settings, first, failure)
    restored = AutomaticFreePool()
    assert restored.cooldown(settings, sibling)["reason"] == "api_access_not_in_plan"
    assert restored.cooldown(settings, model("cline_pass", "working")) is None
    assert (
        restored.cooldown(
            settings.model_copy(update={"commandcode_api_key": "rotated-synthetic"}),
            sibling,
        )
        is None
    )


@pytest.mark.parametrize(
    "provider,body,status",
    [
        ("cline_pass", denial(), 403),
        ("commandcode", {"error": {"code": "model_forbidden"}}, 403),
        ("commandcode", {"error": {"message": "upgrade_required"}}, 403),
        ("commandcode", denial(), 500),
        ("commandcode", "invalid json", 403),
    ],
)
def test_generic_errors_do_not_block_all_models(provider, body, status):
    assert plan_access_failure(provider, body, status) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_large_preferred_catalog_reserves_other_providers(selected):
    settings = Settings(
        allow_paid_api_models=True,
        routing_provider_priority="open_router,kilo,deepseek",
        routing_selected_provider="open_router" if selected else None,
    )
    first = [
        model("open_router", f"deepseek-v4.1-flash-{i}:free", "zero_price")
        for i in range(20)
    ]
    other = model("kilo", "unpreferred:free", "zero_price")
    paid = model("deepseek", "paid")
    pool = freeze_pool(AutomaticFreePool(), [*first, other, paid])
    chosen = await pool.select(settings, {})
    assert len(chosen) == 12
    assert chosen[0].provider_id == "open_router"
    assert other in chosen and chosen[-1] == paid
    assert len({m.ref for m in chosen}) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["messages", "responses"])
@pytest.mark.parametrize("failure_mode", ["502", "eof", "heartbeat_timeout"])
async def test_executor_fails_over_after_headers_without_leaking_them(
    wire, failure_mode
):
    models = [
        model("cline_pass", "broken"),
        model("cline_pass", "sibling"),
        model("deepseek", "working"),
    ]
    settings = Settings(allow_paid_api_models=True, reasoning_policy="max")
    pool = freeze_pool(AutomaticFreePool(), models)
    calls = []
    start = "message_start" if wire == "messages" else "response.created"
    stop = "message_stop" if wire == "messages" else "response.completed"

    async def stream(request, **kwargs):
        calls.append(request.model)
        if request.model == "broken":
            yield (
                "data: "
                + json.dumps({"type": start, "id": "DO_NOT_FORWARD_FAILED_HEADER"})
                + "\n\n"
            )
            if failure_mode == "502":
                raise ExecutionFailure(FailureKind.UPSTREAM, 502, "Bad gateway", False)
            if failure_mode == "heartbeat_timeout":
                while True:
                    await asyncio.sleep(0.01)
                    yield 'data: {"type":"ping"}\n\n'
            return
        assert request.model == "working"
        yield "data: " + json.dumps({"type": start, "id": "good"}) + "\n\n"
        if wire == "messages":
            yield 'data: {"type":"content_block_delta","delta":{"text":"ANSWER"}}\n\n'
        else:
            yield 'data: {"type":"response.output_text.delta","delta":"ANSWER"}\n\n'
        yield "data: " + json.dumps({"type": stop}) + "\n\n"

    async def resolve(_):
        return SimpleNamespace(stream_messages=stream, stream_responses=stream)

    executor = ProviderExecutor(
        resolve,
        progress_timeout_seconds=0.035 if failure_mode == "heartbeat_timeout" else 1,
    )
    executor.configure_free_routing(pool, settings, models)
    router = ModelRouter(settings, free_targets=tuple(m.ref for m in models))
    request = (
        MessagesRequest(
            model="auto", max_tokens=1024, messages=[{"role": "user", "content": "hi"}]
        )
        if wire == "messages"
        else OpenAIResponsesRequest(model="auto", input="hi", stream=True)
    )
    routed = getattr(router, "resolve_" + wire + "_request")(request)
    result = "".join(
        [
            chunk
            async for chunk in getattr(executor, "stream_" + wire)(
                routed, raw_log_payload={}, request_id="synthetic"
            )
        ]
    )
    assert "ANSWER" in result and "DO_NOT_FORWARD" not in result
    assert calls == ["broken", "working"]
    assert pool._last_success == "deepseek/working"


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "id": "call", "name": "test"},
        },
        {"type": "content_block_delta", "delta": {"thinking": "reasoning"}},
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "name": "test"},
        },
        {"choices": [{"delta": {"tool_calls": [{"id": "call"}]}}]},
    ],
)
def test_tool_calls_and_reasoning_count_as_committed_output(event):
    check = FreeStreamCheck("cline_pass")
    check.feed("data: " + json.dumps(event) + "\n\n")
    assert check.has_output


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize("stream", [False, True])
def test_http_chat_skips_plan_denial_and_gateway_error(monkeypatch, stream):
    from free_claude_code.api import free_chat_routes

    settings = Settings(
        allow_paid_api_models=True,
        reasoning_policy="max",
        commandcode_api_key="synthetic",
        cline_api_key="synthetic",
        deepseek_api_key="synthetic",
        routing_provider_priority="commandcode,cline_pass,deepseek",
        routing_selected_provider="commandcode",
        routing_selected_billing="paid_api",
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
            *(model("commandcode", f"blocked-{i}") for i in range(15)),
            model("cline_pass", "bad-gateway"),
            model("cline_pass", "sibling"),
            model("deepseek", "works"),
        ],
    )
    sent = []

    def serve(request):
        sent.append(request.url.host)
        if request.url.host == "api.commandcode.ai":
            return httpx.Response(403, json=denial())
        if "cline" in request.url.host:
            if stream:
                return httpx.Response(
                    200,
                    stream=Chunks(
                        [
                            b'data: {"id":"DO_NOT_FORWARD","choices":[{"delta":{"role":"assistant"}}]}\n\n',
                            b'data: {"error":{"code":502,"message":"bad gateway"}}\n\n',
                        ]
                    ),
                )
            return httpx.Response(502, json={"error": "bad gateway"})
        assert request.url.host == "api.deepseek.com"
        if stream:
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"ANSWER"}}]}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ANSWER"}}]}
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        free_chat_routes.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(serve), **kwargs),
    )
    with TestClient(app) as client:
        result = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={"messages": [{"role": "user", "content": "hi"}], "stream": stream},
        )
    assert result.status_code == 200 and "ANSWER" in result.text
    assert "DO_NOT_FORWARD" not in result.text and "DO_NOT_ECHO" not in result.text
    assert (
        len(sent) == 3
        and sent[0] == "api.commandcode.ai"
        and sent[-1] == "api.deepseek.com"
    )
    assert (
        app.state.free_pool.cooldown(settings, model("commandcode", "blocked-14"))[
            "reason"
        ]
        == "api_access_not_in_plan"
    )
