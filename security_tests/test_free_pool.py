"""Policy, discovery, billing guard and cross-request quota regression tests."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from free_helpers import freeze_pool, model
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.application.free_pool import AutomaticFreePool
from free_claude_code.config.free_providers import (
    POLICY_BY_ID,
    explicitly_zero_priced,
    zen_free_chat_ids,
)
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.free_accounts import FreeAccountConfirmations
from free_claude_code.core.free_stream import FreeStreamCheck, retry_seconds
from free_claude_code.providers.direct_chat import prepare_chat_body


@pytest.mark.parametrize(
    "prices,expected",
    [
        ({"prompt": "0", "completion": "0", "request": "0"}, True),
        ({"prompt": 0, "completion": 0, "image": "0.01"}, False),
        ({"prompt": 0, "completion": 0, "cache_write": "1"}, False),
        ({"prompt": 0}, False),
        ({}, False),
        ({"prompt": False, "completion": 0}, False),
        ({"prompt": "NaN", "completion": 0}, False),
        ({"prompt": "-1", "completion": 0}, False),
    ],
)
def test_all_published_prices_must_be_explicitly_zero(prices, expected):
    assert explicitly_zero_priced({"pricing": prices}) is expected


def test_zero_price_all_tiers_and_zen_endpoint_join():
    assert explicitly_zero_priced(
        {"pricings": {"prompt": [{"value": 0}], "completion": [{"value": 0}]}}
    )
    assert not explicitly_zero_priced(
        {
            "pricings": {
                "prompt": [{"value": 0}, {"value": 1}],
                "completion": [{"value": 0}],
            }
        }
    )
    html = "<table><tr><td>Good</td><td>Free</td><td>Free</td><td>—</td></tr><tr><td>Good</td><td>good-free</td><td>https://opencode.ai/zen/v1/chat/completions</td></tr><tr><td>Paid</td><td>paid-free</td><td>https://opencode.ai/zen/v1/chat/completions</td></tr><tr><td>Good</td><td>wrong-protocol</td><td>https://opencode.ai/zen/v1/responses</td></tr></table>"
    assert zen_free_chat_ids(html) == {"good-free"}


def test_free_account_confirmation_binds_key_and_can_be_revoked():
    accounts = FreeAccountConfirmations()
    settings = Settings(groq_api_key="synthetic-one")
    assert not accounts.confirmed(settings, "groq")
    accounts.set(settings, "groq", True)
    assert accounts.confirmed(settings, "groq")
    changed = settings.model_copy(update={"groq_api_key": "synthetic-two"})
    assert not accounts.confirmed(changed, "groq")
    accounts.set(settings, "groq", False)
    assert not accounts.confirmed(settings, "groq")
    with pytest.raises(ValueError):
        accounts.set(Settings(), "groq", True)
    with pytest.raises(ValueError):
        accounts.set(settings, "open_router", True)


def row(name, context=1048576, **extra):
    return {
        "id": name,
        "context_length": context,
        "supported_parameters": ["tools"],
        "pricing": {"prompt": "0", "completion": "0"},
        **extra,
    }


@pytest.mark.asyncio
async def test_discovery_filters_paid_and_unknown_models_then_enforces_512k(
    monkeypatch,
):
    pool = AutomaticFreePool()
    settings = Settings(
        open_router_api_key="synthetic", ollama_base_url="https://untrusted.example"
    )
    catalogs = [
        row("large"),
        row("exact", 512000),
        row("small", 511999),
        row("unknown", None),
        row("paid", pricing={"prompt": 0, "completion": 1}),
        row("bad-free", pricing={}),
        row("no-tools", supported_parameters=[]),
        row("openrouter/free"),
        row("narrow-provider", top_provider={"context_length": 128000}),
    ]

    async def fetch(client, url, **kwargs):
        if url == "https://models.dev/api.json":
            return b"{}"
        if url.startswith("https://openrouter.ai/"):
            return json.dumps({"data": catalogs}).encode()
        raise httpx.ConnectError("synthetic unavailable")

    monkeypatch.setattr(pool, "_fetch", fetch)
    status = await pool.status(settings)
    assert status["minimum_context_tokens"] == 512000
    assert status["eligible_models"] == 2
    assert {m.model_id for m in pool._catalog} == {"large", "exact"}
    assert (
        next(r for r in status["providers"] if r["provider"] == "ollama")["state"]
        == "DISCOVERY_UNAVAILABLE"
    )
    catalogs[:] = [row("large", pricing={"prompt": 1, "completion": 1})]
    await pool.refresh(settings, force=True)
    assert pool._catalog == ()


@pytest.mark.asyncio
async def test_gemini_native_header_pagination_and_foreign_link_rejected(monkeypatch):
    pool = AutomaticFreePool()
    FreeAccountConfirmations().set(Settings(gemini_api_key="synthetic"), "gemini", True)
    seen = []

    def serve(req):
        seen.append(req)
        assert req.headers["x-goog-api-key"] == "synthetic"
        assert "authorization" not in req.headers and "synthetic" not in str(req.url)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-big",
                        "inputTokenLimit": 1048576,
                        "supportedGenerationMethods": ["generateContent"],
                        "capabilities": {"tools": True},
                    }
                ],
                **({} if "pageToken" in req.url.params else {"nextPageToken": "page2"}),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        models, complete = await pool._discover(
            client, Settings(gemini_api_key="synthetic"), POLICY_BY_ID["gemini"], {}
        )
    assert complete and len(models) == 1 and len(seen) == 2

    async def malicious(client, url, **kwargs):
        return json.dumps(
            {"data": [], "links": {"next": "https://attacker.example/models"}}
        ).encode()

    monkeypatch.setattr(pool, "_fetch", malicious)
    with pytest.raises(ValueError, match="left the provider"):
        await pool._discover(
            None,
            Settings(open_router_api_key="secret"),
            POLICY_BY_ID["open_router"],
            {},
        )


@pytest.mark.asyncio
async def test_context_vision_request_fit_and_provider_diversity():
    pool = freeze_pool(
        AutomaticFreePool(),
        [model(name=f"or-{n}") for n in range(25)]
        + [model("gemini", "vision", vision=True), model("groq", "too-small", 511999)],
    )
    settings = Settings()
    selected = await pool.select(
        settings, {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert len(selected) == 12 and selected[1].provider_id == "gemini"
    assert all(m.context >= 512000 for m in selected)
    selected = await pool.select(
        settings,
        {
            "messages": [
                {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,abc"},
                        }
                    ]
                }
            ]
        },
    )
    assert [m.provider_id for m in selected] == ["gemini"]
    with pytest.raises(ExecutionFailure, match="512,000"):
        await pool.select(settings, {"messages": [{"content": "x" * 2200000}]})


def test_provider_cooldown_survives_restart_expiry_and_key_rotation(monkeypatch):
    from free_claude_code.application import free_pool

    now = [1000000.0]
    monkeypatch.setattr(free_pool, "time", lambda: now[0])
    settings = Settings(open_router_api_key="synthetic-one")
    first, second = model(name="a"), model(name="b")
    pool = AutomaticFreePool()
    failure = ExecutionFailure(FailureKind.RATE_LIMIT, 429, "quota", False, 7200)
    pool.record_failure(settings, first, failure)
    pool = AutomaticFreePool()
    assert pool.cooldown(settings, second)["until"] == now[0] + 7200
    assert not pool.cooldown(
        settings.model_copy(update={"open_router_api_key": "changed"}), second
    )
    now[0] += 7201
    assert not pool.cooldown(settings, first)


@pytest.mark.parametrize(
    "value,expected",
    [("120", 120), ("NaN", None), ("garbage", None), ("999999", 172800)],
)
def test_retry_after_is_bounded(value, expected):
    assert retry_seconds({"retry-after": value}) == expected


@pytest.mark.parametrize("wire", ["messages", "responses"])
@pytest.mark.asyncio
async def test_executor_independent_fallback_and_no_replay_after_output(wire):
    from free_claude_code.application.execution import ProviderExecutor
    from free_claude_code.application.routing import ModelRouter
    from free_claude_code.core.anthropic import MessagesRequest
    from free_claude_code.core.openai_responses import OpenAIResponsesRequest

    settings = Settings()
    models = (
        model(name="one"),
        model(name="same-provider"),
        model("gemini", "independent"),
    )
    pool = freeze_pool(AutomaticFreePool(), models)
    calls = []
    terminal = "message_stop" if wire == "messages" else "response.completed"
    committed = [False]

    async def stream(request, **kwargs):
        calls.append(request.model)
        if request.model == "one":
            if committed[0]:
                yield 'data: {"type":"content_block_delta","delta":{"text":"partial"}}\n\n'
            raise ExecutionFailure(FailureKind.RATE_LIMIT, 429, "quota", False, 3600)
        assert request.model == "independent"
        yield "data: " + json.dumps({"type": terminal}) + "\n\n"

    async def resolve(provider):
        return SimpleNamespace(stream_messages=stream, stream_responses=stream)

    executor = ProviderExecutor(resolve, progress_timeout_seconds=0.5)
    executor.configure_free_routing(pool, settings, models)
    router = ModelRouter(settings, free_targets=tuple(m.ref for m in models))
    request = (
        MessagesRequest(
            model="paid-ignored",
            max_tokens=90000,
            messages=[{"role": "user", "content": "hi"}],
        )
        if wire == "messages"
        else OpenAIResponsesRequest(model="paid-ignored", input="hi", stream=True)
    )
    routed = getattr(router, "resolve_" + wire + "_request")(request)
    execute = getattr(executor, "stream_" + wire)
    result = [x async for x in execute(routed, raw_log_payload={}, request_id="test")]
    assert terminal in "".join(result) and calls == ["one", "independent"]
    assert pool.cooldown(settings, models[1])
    pool._cooldowns.clear()
    calls.clear()
    committed[0] = True
    with pytest.raises(ExecutionFailure):
        [
            x
            async for x in execute(
                routed, raw_log_payload={}, request_id="test-committed"
            )
        ]
    assert calls == ["one"]


@pytest.mark.asyncio
async def test_executor_timeout_before_output_can_fall_back():
    from free_claude_code.application.execution import ProviderExecutor
    from free_claude_code.application.routing import ModelRouter
    from free_claude_code.core.anthropic import MessagesRequest

    settings = Settings()
    models = (model(), model("gemini", "works"))
    pool = freeze_pool(AutomaticFreePool(), models)
    calls = []

    async def stream(request, **kwargs):
        calls.append(request.model)
        if request.model == "free-big":
            await asyncio.sleep(0.2)
        yield 'data: {"type":"message_stop"}\n\n'

    async def resolve(provider):
        return SimpleNamespace(stream_messages=stream)

    executor = ProviderExecutor(resolve, progress_timeout_seconds=0.025)
    executor.configure_free_routing(pool, settings, models)
    routed = ModelRouter(
        settings, free_targets=tuple(m.ref for m in models)
    ).resolve_messages_request(
        MessagesRequest(
            model="auto", max_tokens=100, messages=[{"role": "user", "content": "hi"}]
        )
    )
    assert [
        x
        async for x in executor.stream_messages(
            routed, raw_log_payload={}, request_id="timeout"
        )
    ]
    assert calls == ["free-big", "works"]


def test_stream_inspector_detects_split_errors_and_completion():
    check = FreeStreamCheck("gemini")
    check.feed(b'data: {"error":')
    with pytest.raises(ExecutionFailure):
        check.feed(b'{"message":"oops"}}\n\n')
    check = FreeStreamCheck("gemini")
    check.feed('data: {"choices":[]}\n\ndata: [DONE]\n\n')
    assert check.complete


@pytest.mark.asyncio
async def test_free_account_catalog_is_not_used_until_confirmed(monkeypatch):
    pool = AutomaticFreePool()
    settings = Settings(groq_api_key="synthetic")
    calls = []

    async def fetch(client, url, **kwargs):
        calls.append(url)
        if url == "https://models.dev/api.json":
            return b"{}"
        if "api.groq.com" in url:
            return json.dumps({"data": [row("synthetic-large")]}).encode()
        raise httpx.ConnectError("synthetic unavailable")

    monkeypatch.setattr(pool, "_fetch", fetch)
    before = await pool.status(settings)
    assert not any("api.groq.com" in url for url in calls)
    assert (
        next(r for r in before["providers"] if r["provider"] == "groq")["state"]
        == "CONFIRM_FREE_ACCOUNT"
    )
    FreeAccountConfirmations().set(settings, "groq", True)
    after = await pool.status(settings)
    assert after["eligible_models"] == 1
    assert any("api.groq.com" in url for url in calls)

    async def down(*args, **kwargs):
        raise httpx.ConnectError("catalog down")

    monkeypatch.setattr(pool, "_fetch", down)
    await pool.refresh(settings, force=True)
    assert pool._catalog == ()


@pytest.mark.asyncio
async def test_local_context_configuration_cannot_expand_model_capacity(monkeypatch):
    pool = AutomaticFreePool()

    async def fetch(client, url, **kwargs):
        if url.endswith("/api/tags"):
            return b'{"models":[{"name":"installed"}]}'
        return json.dumps(
            {
                "capabilities": ["tools"],
                "parameters": "num_ctx 1048576",
                "model_info": {"test.context_length": 131072},
            }
        ).encode()

    monkeypatch.setattr(pool, "_fetch", fetch)
    models, complete = await pool._discover_local(None, Settings(), "ollama")
    assert complete and models[0].context == 131072
    freeze_pool(pool, models)
    with pytest.raises(ExecutionFailure, match="512,000"):
        await pool.select(Settings(), {"messages": [{"content": "hi"}]})


def test_factory_free_mode_limits_attempts_and_pins_cloud_destination():
    from free_claude_code.providers.runtime.factory import prepare_provider

    captured = []

    def construct(config, settings, admission):
        captured.append((config, admission))
        return SimpleNamespace(_behavior=SimpleNamespace())

    provider = prepare_provider("open_router", {"open_router": lambda: construct})(
        Settings(open_router_api_key="synthetic", http_read_timeout=120)
    )
    config, admission = captured[0]
    assert (
        config.base_url == "https://openrouter.ai/api/v1"
        and config.http_read_timeout == 45
        and config.proxy is None
    )
    assert provider._behavior.free_only is True
    assert admission._max_attempts == 1


@pytest.mark.parametrize("stream", [False, True])
def test_chat_cross_provider_failover_and_shared_cooldown(monkeypatch, stream):
    from free_claude_code.api import free_chat_routes

    settings = Settings(
        open_router_api_key="synthetic-or", gemini_api_key="synthetic-gemini"
    )
    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(current_settings=lambda: settings),
            admin=SimpleNamespace(admin_status=None),
            prepare_chat_body=prepare_chat_body,
        )
    )
    pool = freeze_pool(
        app.state.free_pool,
        (model(), model(name="same-quota"), model("gemini", "independent")),
    )
    calls = []

    def serve(req):
        calls.append(req)
        if req.url.host == "openrouter.ai":
            return httpx.Response(
                429,
                json={"error": {"code": 429, "message": "free-models-per-day"}},
                headers={"retry-after": "86400"},
            )
        assert req.headers["Authorization"] == "Bearer synthetic-gemini"
        body = json.loads(req.content)
        assert (
            body["model"] == "independent"
            and "provider" not in body
            and "plugins" not in body
        )
        if stream:
            return httpx.Response(
                200,
                content='data: {"choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        free_chat_routes.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(serve), **kwargs),
    )
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + settings.proxy_auth_token},
                json={
                    "model": "paid",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": stream,
                    "plugins": [{"id": "paid"}],
                },
            )
            assert response.status_code == 200 and "done" in response.text
    assert [r.url.host for r in calls] == [
        "openrouter.ai",
        "generativelanguage.googleapis.com",
        "generativelanguage.googleapis.com",
    ]
    assert pool.cooldown(settings, model(name="same-quota"))


def test_admin_free_status_requires_session_and_proxy_status_requires_token():
    settings = Settings()
    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(current_settings=lambda: settings),
            admin=SimpleNamespace(admin_status=None),
            prepare_chat_body=prepare_chat_body,
        )
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 4321),
        follow_redirects=False,
    ) as client:
        assert client.get("/admin/free").status_code in {401, 303, 307}
        assert client.get("/admin/api/free/status").status_code == 401
        assert client.post(
            "/admin/api/free/accounts/groq", json={"no_paid_billing": True}
        ).status_code in {401, 403}
        assert client.get("/v1/free/status").status_code == 401


@pytest.mark.parametrize("wire", ["messages", "responses"])
def test_http_messages_responses_routes_choose_free_pool_and_strip_paid_extras(wire):
    from unittest.mock import AsyncMock

    settings = Settings(
        open_router_api_key="synthetic", gemini_api_key="synthetic-gemini"
    )
    calls = []

    async def stream(request, **kwargs):
        calls.append(request)
        if request.model == "free-big":
            raise ExecutionFailure(
                FailureKind.RATE_LIMIT, 429, "free pool quota", False
            )
        assert request.model == "independent"
        assert not request.model_extra
        if wire == "messages":
            assert request.extra_body is None
            assert request.max_tokens <= 4096
            for event in (
                {
                    "type": "message_start",
                    "message": {
                        "id": "test",
                        "type": "message",
                        "role": "assistant",
                        "model": "auto",
                        "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "synthetic-success"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ):
                yield (
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                )
        else:
            assert request.max_output_tokens <= 4096
            yield 'event: response.completed\ndata: {"type":"response.completed","response":{"id":"test","status":"completed","output":[]}}\n\n'

    async def resolve(provider):
        return SimpleNamespace(stream_messages=stream, stream_responses=stream)

    lease = SimpleNamespace(
        settings=settings,
        generation_id=1,
        wait_for_token_estimation=AsyncMock(),
        release=AsyncMock(),
        model_info=lambda *args: None,
        is_provider_cached=lambda _: True,
        resolve_provider=resolve,
    )
    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(
                current_settings=lambda: settings, acquire=AsyncMock(return_value=lease)
            ),
            admin=SimpleNamespace(admin_status=None),
            prepare_chat_body=prepare_chat_body,
            web_tools=SimpleNamespace(),
        )
    )
    freeze_pool(
        app.state.free_pool,
        (model(), replace(model("gemini", "independent"), output_limit=4096)),
    )
    payload = {
        "model": "paid-model",
        "plugins": [{"id": "paid-web"}],
        "provider": {"max_price": {"prompt": 999}},
        "stream": True,
    }
    if wire == "messages":
        payload.update(
            messages=[{"role": "user", "content": "Write a synthetic greeting"}],
            max_tokens=90000,
            extra_body={"model": "paid-model"},
        )
    else:
        payload.update(input="Write a synthetic greeting", max_output_tokens=90000)
    with TestClient(app) as client:
        result = client.post(
            "/v1/" + wire,
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json=payload,
        )
    assert result.status_code == 200, result.text
    assert (
        "synthetic-success" if wire == "messages" else "response.completed"
    ) in result.text
    assert [r.model for r in calls] == ["free-big", "independent"]
    assert lease.release.await_count == 1


@pytest.mark.asyncio
async def test_read_only_quota_preflight_cools_all_siblings_and_rechecks(monkeypatch):
    pool = AutomaticFreePool()
    settings = Settings(open_router_api_key="synthetic")
    quota = [0]

    async def fetch(client, url, **kwargs):
        if url.endswith("/key"):
            return json.dumps(
                {
                    "data": {
                        "free_model_daily_requests": {
                            "remaining": quota[0],
                            "limit": 50,
                        }
                    }
                }
            ).encode()
        return json.dumps({"data": [row("free-one"), row("free-two")]}).encode()

    monkeypatch.setattr(pool, "_fetch", fetch)
    models, _ = await pool._discover(None, settings, POLICY_BY_ID["open_router"], {})
    assert len(models) == 2 and all(pool.cooldown(settings, m) for m in models)
    quota[0] = 50
    await pool._discover(None, settings, POLICY_BY_ID["open_router"], {})
    assert not any(pool.cooldown(settings, m) for m in models)


def test_model_access_denial_does_not_disable_other_models():
    pool = AutomaticFreePool()
    settings = Settings()
    pool.record_failure(
        settings,
        model(name="restricted"),
        ExecutionFailure(FailureKind.PERMISSION, 403, "no model access", False),
    )
    assert pool.cooldown(settings, model(name="restricted"))
    assert not pool.cooldown(settings, model(name="another-model"))


@pytest.mark.asyncio
async def test_mistral_primary_limits_override_secondary_capabilities(monkeypatch):
    pool = AutomaticFreePool()

    async def fetch(*args, **kwargs):
        return json.dumps(
            {
                "data": [
                    {
                        "id": "primary-large",
                        "max_context_length": 512000,
                        "max_output_tokens": 4096,
                        "capabilities": {"function_calling": True},
                    },
                    {
                        "id": "no-tools",
                        "max_context_length": 1048576,
                        "capabilities": {"function_calling": False},
                    },
                ]
            }
        ).encode()

    monkeypatch.setattr(pool, "_fetch", fetch)
    FreeAccountConfirmations().set(
        Settings(mistral_api_key="synthetic"), "mistral", True
    )
    models, _ = await pool._discover(
        None,
        Settings(mistral_api_key="synthetic"),
        POLICY_BY_ID["mistral"],
        {"mistral": {"models": {"no-tools": {"tool_call": True}}}},
    )
    assert (
        len(models) == 1
        and models[0].context == 512000
        and models[0].output_limit == 4096
    )
