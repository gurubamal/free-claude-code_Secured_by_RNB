"""Billing rejections rotate automatically without blocking affordable siblings."""

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
from free_claude_code.core.billing_limits import openrouter_billing_failure
from free_claude_code.core.failures import BillingLimit, ExecutionFailure, FailureKind
from free_claude_code.core.free_stream import FreeStreamCheck
from free_claude_code.core.openai_responses import OpenAIResponsesRequest
from free_claude_code.providers.direct_chat import prepare_chat_body
from free_claude_code.providers.failure_policy import classify_provider_failure


def billing_error(source="openrouter_credits"):
    return {
        "error": {
            "code": 402,
            "message": "Prompt tokens limit exceeded: 120000 > 90000. "
            "Account details: https://example.invalid/PRIVATE_ACCOUNT_URL",
            "metadata": {
                "limit_source": source,
                "remedy_hint": "DO_NOT_ECHO_PRIVATE_DETAIL",
                "previous_errors": [{"message": "DO_NOT_ECHO_HISTORY"}],
            },
        }
    }


def model(provider, name, billing="paid_api"):
    return FreeModel(provider, name, 1048576, 8192, True, False, billing)


def sdk_failure(body, status=402, headers=None):
    error = openai.APIStatusError(
        "rejected",
        response=httpx2.Response(
            status,
            request=httpx2.Request(
                "POST", "https://openrouter.ai/api/v1/chat/completions"
            ),
            headers=headers,
        ),
        body=body,
    )
    return classify_provider_failure(
        error, provider_name="OPENROUTER", read_timeout_s=45, request_id="synthetic"
    )


@pytest.mark.parametrize("unwrapped", [False, True])
@pytest.mark.parametrize(
    "source,expected",
    [
        ("openrouter_credits", BillingLimit.REQUEST_BUDGET),
        ("openrouter_key_limit", BillingLimit.KEY_LIMIT),
        ("openrouter_in_flight_budget", BillingLimit.IN_FLIGHT_BUDGET),
    ],
)
def test_sdk_billing_scope_is_preserved_without_echoing_account_data(
    source, expected, unwrapped
):
    body = billing_error(source)
    failure = sdk_failure(
        body["error"] if unwrapped else body, headers={"retry-after": "17"}
    )
    assert failure.billing_limit is expected and failure.status_code == 402
    assert not failure.retryable and failure.kind is FailureKind.PERMISSION
    assert failure.retry_after_seconds == 17
    assert "PRIVATE" not in failure.message and "HISTORY" not in failure.message
    assert "https://" not in failure.message


def test_http_error_body_is_read_and_legacy_prompt_budget_is_not_context_overflow():
    body = billing_error()
    body["error"].pop("metadata")
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    error = httpx.HTTPStatusError(
        "rejected",
        request=request,
        response=httpx.Response(402, request=request, json=body),
    )
    failure = classify_provider_failure(
        error, provider_name="OPENROUTER", read_timeout_s=45, request_id=None
    )
    assert failure.billing_limit is BillingLimit.REQUEST_BUDGET
    assert failure.kind is FailureKind.PERMISSION


@pytest.mark.parametrize(
    "body",
    [
        "invalid json",
        "x" * 65537,
        {"error": []},
        {
            "error": {
                "code": [],
                "message": "Prompt tokens limit exceeded: 10 > 5",
                "metadata": {"limit_source": []},
            }
        },
        {"error": {"metadata": {"previous_errors": [billing_error()]}}},
        billing_error("unknown_source"),
    ],
    ids=[
        "invalid-json",
        "oversize",
        "invalid-error",
        "invalid-fields",
        "history-only",
        "unknown-source",
    ],
)
def test_malformed_unknown_and_history_only_billing_errors_remain_unspecified(body):
    failure = openrouter_billing_failure("open_router", body, 402)
    assert failure.billing_limit is None and "PRIVATE" not in failure.message
    assert openrouter_billing_failure("deepseek", body, 402) is None
    assert openrouter_billing_failure("open_router", body, 403) is None


@pytest.mark.parametrize("code", [[], {}, True, None])
def test_malformed_stream_code_is_not_a_billing_rejection(code):
    assert (
        openrouter_billing_failure("open_router", {"error": {"code": code}}, 200)
        is None
    )


@pytest.mark.parametrize("source", ["openrouter_credits", "openrouter_key_limit"])
def test_sse_billing_scope_survives_stream_parser(source):
    with pytest.raises(ExecutionFailure) as caught:
        FreeStreamCheck("open_router").feed(
            "data: " + json.dumps(billing_error(source)) + "\n\n"
        )
    assert caught.value.billing_limit is (
        BillingLimit.REQUEST_BUDGET
        if source == "openrouter_credits"
        else BillingLimit.KEY_LIMIT
    )


def test_request_budget_only_cools_failed_model_and_survives_reload():
    settings = Settings(allow_paid_api_models=True, open_router_api_key="synthetic")
    expensive = model("open_router", "expensive")
    pool = AutomaticFreePool()
    pool.record_failure(settings, expensive, sdk_failure(billing_error()))
    restored = AutomaticFreePool()
    assert restored.cooldown(settings, expensive)["reason"] == "request_budget_exceeded"
    for other in (
        model("open_router", "affordable"),
        model("open_router", "available:free", "zero_price"),
        model("deepseek", "alternative"),
    ):
        assert restored.cooldown(settings, other) is None


@pytest.mark.parametrize(
    "source,reason,seconds",
    [
        ("openrouter_key_limit", "key_spending_limit", 3600),
        ("openrouter_in_flight_budget", "in_flight_budget", 17),
    ],
)
def test_account_scopes_skip_paid_siblings_and_honor_retry_after(
    monkeypatch, source, reason, seconds
):
    monkeypatch.setattr("free_claude_code.application.free_pool.time", lambda: 10000)
    settings = Settings(allow_paid_api_models=True)
    pool = AutomaticFreePool()
    pool.record_failure(
        settings,
        model("open_router", "one"),
        sdk_failure(billing_error(source), headers={"retry-after": "17"}),
    )
    entry = pool.cooldown(settings, model("open_router", "two"))
    assert entry["reason"] == reason and entry["until"] == 10000 + seconds
    assert (
        pool.cooldown(settings, model("open_router", "free:free", "zero_price")) is None
    )
    assert pool.cooldown(settings, model("deepseek", "working")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["messages", "responses"])
@pytest.mark.parametrize("other_works", [False, True])
async def test_executor_rotates_to_other_provider_then_affordable_sibling(
    wire, other_works
):
    settings = Settings(allow_paid_api_models=True)
    models = [
        model("open_router", "expensive"),
        model("open_router", "affordable"),
        model("deepseek", "other"),
    ]
    pool = freeze_pool(AutomaticFreePool(), models)
    calls, seen = [], []
    original = "PRESERVE_PROMPT_AND_TOOL_CONTEXT" * 4000
    start = "message_start" if wire == "messages" else "response.created"
    stop = "message_stop" if wire == "messages" else "response.completed"

    async def stream(request, **kwargs):
        calls.append(request.model)
        seen.append(request.model_dump(exclude={"model"}))
        if request.model == "expensive":
            raise sdk_failure(billing_error())
        if request.model == "other" and not other_works:
            raise ExecutionFailure(
                FailureKind.PERMISSION, 402, "Billing unavailable", False
            )
        yield "data: " + json.dumps({"type": start}) + "\n\n"
        event = (
            {"type": "content_block_delta", "delta": {"text": "ANSWER"}}
            if wire == "messages"
            else {"type": "response.output_text.delta", "delta": "ANSWER"}
        )
        yield "data: " + json.dumps(event) + "\n\n"
        yield "data: " + json.dumps({"type": stop}) + "\n\n"

    async def resolve(_):
        return SimpleNamespace(stream_messages=stream, stream_responses=stream)

    executor = ProviderExecutor(resolve, progress_timeout_seconds=1)
    executor.configure_free_routing(pool, settings, models)
    router = ModelRouter(settings, free_targets=tuple(m.ref for m in models))
    request = (
        MessagesRequest(
            model="auto",
            max_tokens=1024,
            messages=[{"role": "user", "content": original}],
            tools=[
                {
                    "name": "read_file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )
        if wire == "messages"
        else OpenAIResponsesRequest(
            model="auto",
            input=original,
            stream=True,
            tools=[
                {
                    "type": "function",
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        )
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
    assert "ANSWER" in result and "PRIVATE" not in result
    assert calls == (
        ["expensive", "other"] if other_works else ["expensive", "other", "affordable"]
    )
    assert all(payload == seen[0] for payload in seen)
    assert original in json.dumps(seen[0]) and seen[0]["tools"]
    assert pool._last_success == (
        "deepseek/other" if other_works else "open_router/affordable"
    )


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        yield ("data: " + json.dumps(billing_error()) + "\n\n").encode()


@pytest.mark.parametrize("mode", ["http", "sse", "json_error"])
def test_chat_request_rotates_after_402_without_discarding_context(monkeypatch, mode):
    from free_claude_code.api import free_chat_routes

    settings = Settings(
        allow_paid_api_models=True,
        open_router_api_key="synthetic",
        deepseek_api_key="synthetic",
        routing_selected_provider="open_router",
        routing_selected_model="expensive",
        routing_selected_billing="paid_api",
    )
    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(current_settings=lambda: settings),
            admin=SimpleNamespace(admin_status=None),
            prepare_chat_body=prepare_chat_body,
        )
    )
    freeze_pool(
        app.state.free_pool,
        [
            model("open_router", "expensive"),
            model("open_router", "affordable"),
            model("deepseek", "other"),
        ],
    )
    sent = []
    messages = [{"role": "user", "content": "PRESERVE_CONTEXT" * 8000}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def serve(request):
        body = json.loads(request.content)
        sent.append(body["model"])
        assert body["messages"] == messages and body["tools"] == tools
        if body["model"] == "expensive":
            if mode == "sse":
                return httpx.Response(200, stream=Chunks())
            return httpx.Response(402 if mode == "http" else 200, json=billing_error())
        if body["model"] == "other":
            return httpx.Response(402, json={"error": {"code": 402}})
        assert body["model"] == "affordable"
        if mode == "sse":
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
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={"messages": messages, "tools": tools, "stream": mode == "sse"},
        )
    assert response.status_code == 200 and "ANSWER" in response.text
    assert "PRIVATE" not in response.text
    assert sent == ["expensive", "other", "affordable"]


@pytest.mark.asyncio
async def test_budget_failure_does_not_enable_paid_routes_or_relax_context():
    settings = Settings()
    pool = freeze_pool(
        AutomaticFreePool(),
        [
            model("open_router", "paid"),
            FreeModel("kilo", "small:free", 512000, 8192, True, False, "zero_price"),
        ],
    )
    with pytest.raises(ExecutionFailure) as caught:
        await pool.select(settings, {})
    assert caught.value.status_code == 503
