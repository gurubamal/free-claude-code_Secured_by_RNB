"""Billing opt-ins, strict context admission and deterministic fallback order."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest
from pydantic import ValidationError

from free_claude_code.api.free_chat_routes import chat_target
from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.config.free_providers import POLICY_BY_ID
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.providers.direct_chat import prepare_chat_body
from free_claude_code.providers.open_router.client import (
    _PROFILE,
    OpenRouterChatBehavior,
)


@pytest.mark.asyncio
async def test_category_priority_precedes_siblings_and_reserves_later_categories(
    monkeypatch,
):
    pool = AutomaticFreePool()
    monkeypatch.setattr(pool, "refresh", AsyncMock())
    settings = Settings(allow_subscription_models=True, allow_paid_api_models=True)
    pool._catalog = (
        *(model("open_router", f"free-{i}:free", "zero_price") for i in range(15)),
        model("deepseek", "paid"),
        model("openai", "large", "subscription"),
    )
    chosen = await pool.select(settings, {})
    assert len(chosen) == 12
    assert [m.billing for m in chosen] == ["free"] * 10 + ["subscription", "paid_api"]


@pytest.mark.parametrize("wire", ["messages", "responses"])
@pytest.mark.parametrize("manual_selection", [False, True])
def test_real_deepseek_adapter_sends_sibling_after_503(
    monkeypatch, wire, manual_selection
):
    from free_helpers import freeze_pool
    from starlette.testclient import TestClient

    from free_claude_code.api.app import create_app
    from free_claude_code.providers.runtime.factory import prepare_provider
    from tests.providers.support import SDKStreamDouble

    first = "deepseek-z-selected" if manual_selection else "deepseek-first"
    second = "deepseek-a-fallback" if manual_selection else "deepseek-second"
    settings = Settings(
        deepseek_api_key="synthetic",
        allow_paid_api_models=True,
        routing_selected_provider="deepseek" if manual_selection else None,
        routing_selected_model=first if manual_selection else None,
        routing_selected_billing="paid_api",
    )
    provider = prepare_provider("deepseek", {})(settings)
    sent = []

    async def chunks():
        for content, finish in [("SIBLING_OK", None), (None, "stop")]:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=content, reasoning_content=None, tool_calls=None
                        ),
                        finish_reason=finish,
                    )
                ],
                usage=None,
            )

    async def create(**body):
        sent.append(body["model"])
        if body["model"] == first:
            raise openai.InternalServerError(
                "Service too busy",
                response=httpx2.Response(
                    503,
                    request=httpx2.Request(
                        "POST", "https://api.deepseek.com/chat/completions"
                    ),
                ),
                body={"message": "Service too busy"},
            )
        return SDKStreamDouble(chunks())

    monkeypatch.setattr(provider._client.chat.completions, "create", create)

    async def resolve(_):
        return provider

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
            web_tools=SimpleNamespace(),
        )
    )
    freeze_pool(
        app.state.free_pool,
        (model("deepseek", first), model("deepseek", second)),
    )
    payload = {"model": "automatic", "stream": True}
    if wire == "messages":
        payload.update(
            messages=[{"role": "user", "content": "Say hello"}],
            max_tokens=128,
            thinking={"type": "disabled"},
        )
    else:
        payload.update(input="Say hello", max_output_tokens=128)
    with TestClient(app) as client:
        result = client.post(
            "/v1/" + wire,
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json=payload,
        )
        client.portal.call(provider.cleanup)
    assert result.status_code == 200, result.text
    assert "SIBLING_OK" in result.text
    assert sent == [first, second]
    assert app.state.free_pool._last_success == "deepseek/" + second


@pytest.mark.asyncio
async def test_outage_is_not_misreported_as_rate_limit(monkeypatch):
    settings = Settings(allow_paid_api_models=True, deepseek_api_key="synthetic")
    pool = AutomaticFreePool()
    monkeypatch.setattr(pool, "refresh", AsyncMock())
    pool._catalog = (model("deepseek", "first"),)
    pool._reports = [
        {"provider": "deepseek", "state": "ELIGIBLE"},
        {
            "provider": "openai",
            "state": "NO_ELIGIBLE_MODELS",
            "below_context_minimum": 5,
        },
    ]
    pool.record_failure(
        settings,
        pool._catalog[0],
        ExecutionFailure(FailureKind.UPSTREAM, 503, "Service busy", False),
    )
    with pytest.raises(ExecutionFailure) as error:
        await pool.select(settings, {})
    assert error.value.status_code == 503
    assert "deepseek: temporary model outage" in error.value.message
    assert "openai: default context below 256,000" in error.value.message


def model(provider, name, billing="paid_api", context=1048576):
    return FreeModel(provider, name, context, 4096, True, False, billing)


@pytest.mark.asyncio
async def test_opt_in_priority_disable_and_context_floor(monkeypatch):
    pool = AutomaticFreePool()
    monkeypatch.setattr(pool, "refresh", AsyncMock())
    pool._catalog = (
        model("open_router", "free:free", "zero_price"),
        model("deepseek", "paid"),
        model("commandcode", "paid"),
        model("openai", "large", "subscription"),
        model("openai", "default-small", "subscription", 272000),
    )
    default = await pool.select(Settings(), {})
    assert [m.provider_id for m in default] == ["open_router"]
    settings = Settings(
        allow_subscription_models=True,
        allow_paid_api_models=True,
        routing_priority="paid_api,subscription,free",
        routing_provider_priority="commandcode,deepseek",
    )
    chosen = await pool.select(settings, {})
    assert [m.provider_id for m in chosen] == [
        "commandcode",
        "deepseek",
        "openai",
        "openai",
        "open_router",
    ]
    assert all(m.context >= 256000 for m in chosen)
    pool.record_success(chosen[-1])
    assert (await pool.select(settings, {}))[0].provider_id == "commandcode"
    disabled = settings.model_copy(update={"routing_disabled_providers": "commandcode"})
    assert (await pool.select(disabled, {}))[0].provider_id == "deepseek"
    assert all(
        m.provider_id != "openai"
        for m in await pool.select(settings, {"_fcc_wire_api": "chat"})
    )


@pytest.mark.asyncio
async def test_free_quota_exhaustion_does_not_block_paid_route_and_paid_balance_falls_back(
    monkeypatch,
):
    settings = Settings(allow_paid_api_models=True, open_router_api_key="synthetic")
    pool = AutomaticFreePool()
    monkeypatch.setattr(pool, "refresh", AsyncMock())
    free = model("open_router", "free:free", "zero_price")
    paid = model("open_router", "paid")
    other = model("deepseek", "paid")
    pool._catalog = (free, paid, other)
    pool.record_failure(
        settings,
        free,
        ExecutionFailure(
            FailureKind.RATE_LIMIT,
            429,
            "daily free-model request quota exhausted",
            False,
        ),
    )
    chosen = await pool.select(settings, {})
    assert free not in chosen and paid in chosen and other in chosen
    pool.record_failure(
        settings,
        paid,
        ExecutionFailure(FailureKind.UNAVAILABLE, 402, "Balance exhausted", False),
    )
    assert await pool.select(settings, {}) == (other,)


def test_free_price_guard_survives_paid_opt_in():
    behavior = OpenRouterChatBehavior(_PROFILE)
    behavior.free_only = False
    free = behavior.prepare_create_body(
        {"model": "demo:free", "messages": [], "max_tokens": 32}
    )
    assert free["extra_body"]["provider"]["max_price"]["prompt"] == 0
    paid = behavior.prepare_create_body({"model": "paid-model", "messages": []})
    assert "extra_body" not in paid
    _, _, body = chat_target(
        Settings(allow_paid_api_models=True),
        model("open_router", "paid-model"),
        {},
        prepare_body=prepare_chat_body,
    )
    assert "provider" not in body


@pytest.mark.asyncio
async def test_connected_catalog_admits_272k_but_not_small_or_unknown_context(
    monkeypatch,
):
    subscriptions = SimpleNamespace(
        identities=AsyncMock(return_value={"openai": "synthetic-account"}),
        discover=AsyncMock(
            return_value=(
                ProviderModelInfo(
                    "small", context_window_tokens=272000, supports_tools=True
                ),
                ProviderModelInfo("unknown", supports_tools=True),
                ProviderModelInfo(
                    "too-small", context_window_tokens=255999, supports_tools=True
                ),
                ProviderModelInfo(
                    "large", context_window_tokens=512001, supports_tools=True
                ),
            )
        ),
    )
    pool = AutomaticFreePool(subscriptions=subscriptions)
    monkeypatch.setattr(pool, "_fetch", AsyncMock(return_value=b"{}"))
    monkeypatch.setattr(pool, "_discover_local", AsyncMock(return_value=([], True)))
    await pool.refresh(Settings(allow_subscription_models=True), force=True)
    assert {m.model_id for m in pool._catalog} == {"large", "small"}
    report = next(r for r in pool._reports if r["provider"] == "openai")
    assert report["below_context_minimum"] == 1 and report["catalog_models"] == 4
    await pool.refresh(Settings(), force=True)
    assert not pool._catalog


@pytest.mark.asyncio
async def test_commandcode_filters_endpoint_and_paid_discovery_needs_opt_in(
    monkeypatch,
):
    pool = AutomaticFreePool()
    payload = {
        "data": [
            {
                "id": "chat-model",
                "context_length": 1000000,
                "capabilities": {"tools": True},
                "supported_endpoints": ["/chat/completions"],
            },
            {
                "id": "messages-only",
                "context_length": 1000000,
                "capabilities": {"tools": True},
                "supported_endpoints": ["/messages"],
            },
        ]
    }
    monkeypatch.setattr(
        pool, "_fetch", AsyncMock(return_value=json.dumps(payload).encode())
    )
    settings = Settings(commandcode_api_key="synthetic", allow_paid_api_models=True)
    models, _ = await pool._discover(None, settings, POLICY_BY_ID["commandcode"], {})
    assert [m.model_id for m in models] == ["chat-model"]
    assert models[0].billing == "paid_api"
    models, _ = await pool._discover(
        None,
        settings.model_copy(update={"allow_paid_api_models": False}),
        POLICY_BY_ID["commandcode"],
        {},
    )
    assert not models


@pytest.mark.parametrize(
    "updates",
    [
        {"routing_priority": "free,free,paid_api"},
        {"routing_provider_priority": "unknown-provider"},
        {"routing_disabled_providers": "openai,openai"},
    ],
)
def test_invalid_priorities_rejected(updates):
    with pytest.raises(ValidationError):
        Settings(**updates)


@pytest.mark.asyncio
async def test_cline_catalog_uses_exact_registry_ids_when_gateway_omits_metadata(
    monkeypatch,
):
    pool = AutomaticFreePool()
    monkeypatch.setattr(
        pool,
        "_fetch",
        AsyncMock(
            return_value=b'{"data":[{"id":"deepseek/exact"},{"id":"unmatched"}]}'
        ),
    )
    metadata = {
        "openrouter": {
            "models": {
                "deepseek/exact": {
                    "tool_call": True,
                    "limit": {"context": 1000000, "output": 4096},
                }
            }
        }
    }
    models, _ = await pool._discover(
        None,
        Settings(cline_api_key="synthetic", allow_paid_api_models=True),
        POLICY_BY_ID["cline_pass"],
        metadata,
    )
    assert [m.model_id for m in models] == ["deepseek/exact"]
    assert models[0].billing == "paid_api" and models[0].context == 1000000


@pytest.mark.asyncio
async def test_temporary_model_failure_still_allows_sibling(monkeypatch):
    settings = Settings(allow_paid_api_models=True, deepseek_api_key="synthetic")
    pool = AutomaticFreePool()
    monkeypatch.setattr(pool, "refresh", AsyncMock())
    first, second = model("deepseek", "first"), model("deepseek", "second")
    pool._catalog = (first, second)
    pool.record_failure(
        settings,
        first,
        ExecutionFailure(FailureKind.UPSTREAM, 503, "Temporary backend outage", False),
    )
    assert await pool.select(settings, {}) == (second,)


def test_routing_controls_require_admin_session_and_validated_update():
    from starlette.testclient import TestClient

    from free_claude_code.api.app import create_app
    from free_claude_code.core.admin_accounts import AdminAccounts

    settings = Settings()
    applied = []

    async def apply(updates):
        nonlocal settings
        try:
            settings = Settings.model_validate(updates)
        except ValidationError:
            return {"valid": False, "errors": ["Invalid policy"]}
        applied.append(updates)
        return {"applied": True, "valid": True}

    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(current_settings=lambda: settings),
            admin=SimpleNamespace(admin_status=None, apply_admin_config=apply),
        )
    )
    app.state.free_pool.status = AsyncMock(
        side_effect=lambda s, **kwargs: {"allow_paid_api": s.allow_paid_api_models}
    )
    password = "Synthetic Routing River 472!"
    AdminAccounts().reset_password(password)
    body = {
        "allow_subscriptions": True,
        "allow_paid_api": True,
        "billing_priority": ["free", "subscription", "paid_api"],
        "provider_priority": ["deepseek", "openai"],
        "disabled_providers": [],
    }
    headers = {"Origin": "http://127.0.0.1", "X-FCC-Admin": "1"}
    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 4321)
    ) as client:
        assert (
            client.post(
                "/admin/api/free/policy", json=body, headers=headers
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/admin/auth/login",
                json={"username": "admin", "password": password},
                headers=headers,
            ).status_code
            == 200
        )
        assert client.post("/admin/api/free/policy", json=body).status_code == 403
        response = client.post("/admin/api/free/policy", json=body, headers=headers)
        assert response.status_code == 200 and response.json()["allow_paid_api"] is True
        assert len(applied) == 1
        body["billing_priority"] = ["paid_api", "paid_api", "free"]
        assert (
            client.post(
                "/admin/api/free/policy", json=body, headers=headers
            ).status_code
            == 400
        )
        assert len(applied) == 1
