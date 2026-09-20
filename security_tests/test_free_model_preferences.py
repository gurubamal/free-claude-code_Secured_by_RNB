"""Free preference must survive fallback without bypassing admission rules."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.config.free_model_preferences import free_model_family
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure, FailureKind


def route(provider, name, *, context=1048576, basis="zero_price", tools=True):
    return FreeModel(provider, name, context, 4096, tools, False, basis)


def pool_with(monkeypatch, *models):
    pool = AutomaticFreePool()
    pool._catalog = models
    monkeypatch.setattr(pool, "refresh", AsyncMock())
    return pool


@pytest.mark.parametrize(
    "name,family",
    [
        ("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek-v4.1-flash"),
        ("deepseek/deepseek-v4.1-flash:free", "deepseek-v4.1-flash"),
        ("DeepSeek-4.1-Flash", "deepseek-v4.1-flash"),
        ("moonshotai/kimi-k3", "kimi-k3"),
        ("qwen/qwen3.8-max-0902", "qwen3.8-max"),
        ("qwen/qwen3.8-max-2026-09-02:free", "qwen3.8-max"),
        ("z-ai/glm-5.3-flash", "glm-5.3-flash"),
        ("deepseek/deepseek-v4-flash:free", None),
        ("deepseek-flash", None),
        ("~deepseek/deepseek-flash-latest", None),
        ("z-ai/glm-5.3-flashx", None),
        ("z-ai/glm-5.3-flash:batch", None),
        ("moonshotai/kimi-k3-preview", None),
        ("qwen/qwen3.8-max-distill", None),
    ],
)
def test_version_identity(name, family):
    assert free_model_family(name) == family


@pytest.mark.asyncio
async def test_model_order_overrides_provider_order_and_last_success(monkeypatch):
    ds = route("nvidia_nim", "deepseek-ai/deepseek-v4.1-flash")
    kimi = route("kilo", "moonshotai/kimi-k3:free")
    qwen = route("open_router", "qwen/qwen3.8-max-0902:free")
    glm = route("open_router", "z-ai/glm-5.3-flash:free")
    other = route("open_router", "other:free")
    pool = pool_with(monkeypatch, other, glm, qwen, kimi, ds)
    pool.record_success(other)
    settings = Settings(routing_provider_priority="open_router,kilo,nvidia_nim")
    for wire in ("messages", "responses", "chat"):
        assert await pool.select(settings, {"_fcc_wire_api": wire}) == (
            ds,
            kimi,
            qwen,
            glm,
            other,
        )


@pytest.mark.asyncio
async def test_failure_then_return_to_deepseek_after_cooldown(monkeypatch):
    ds = route("nvidia_nim", "deepseek-v4.1-flash")
    kimi = route("kilo", "kimi-k3:free")
    pool = pool_with(monkeypatch, ds, kimi)
    settings = Settings()
    pool.record_failure(
        settings, ds, ExecutionFailure(FailureKind.UPSTREAM, 503, "busy", False)
    )
    assert await pool.select(settings, {}) == (kimi,)
    pool.record_success(kimi)
    # Expire the recorded cooldown; a successful fallback must not become sticky.
    for entry in pool._cooldowns.values():
        entry["until"] = 0
    assert await pool.select(settings, {}) == (ds, kimi)


@pytest.mark.asyncio
async def test_admission_and_paid_order_are_preserved(monkeypatch):
    ds_paid = route("deepseek", "deepseek-v4.1-flash", basis="paid_api")
    too_small = route("nvidia_nim", "deepseek-v4.1-flash", context=128000)
    no_tools = route("kilo", "deepseek-v4.1-flash:free", tools=False)
    kimi = route("open_router", "kimi-k3:free")
    pool = pool_with(monkeypatch, ds_paid, too_small, no_tools, kimi)
    request = {"tools": [{"name": "read"}]}
    assert await pool.select(Settings(), request) == (kimi,)
    settings = Settings(
        allow_paid_api_models=True, routing_priority="paid_api,free,subscription"
    )
    assert await pool.select(settings, request) == (ds_paid, kimi)
    settings = Settings(routing_disabled_providers="open_router")
    with pytest.raises(ExecutionFailure):
        await pool.select(settings, request)


@pytest.mark.asyncio
async def test_many_preferred_routes_reserve_paid_and_subscription_fallback(
    monkeypatch,
):
    free = [route("open_router", f"deepseek-v4.1-flash-{i:04}:free") for i in range(20)]
    paid = route("deepseek", "paid", basis="paid_api")
    subscription = route("openai", "large", basis="subscription")
    pool = pool_with(monkeypatch, *free, paid, subscription)
    selected = await pool.select(
        Settings(allow_paid_api_models=True, allow_subscription_models=True), {}
    )
    assert len(selected) == 12
    assert selected[-2:] == (subscription, paid)


@pytest.mark.asyncio
async def test_editable_priority_and_status_do_not_claim_paid_is_free(monkeypatch):
    ds = route("deepseek", "deepseek-v4.1-flash", basis="paid_api")
    kimi = route("kilo", "kimi-k3:free")
    glm = route("open_router", "glm-5.3-flash:free")
    pool = pool_with(monkeypatch, ds, kimi, glm)
    settings = Settings(free_model_priority="glm-5.3-flash,kimi-k3,deepseek-v4.1-flash")
    assert await pool.select(settings, {}) == (glm, kimi)
    status = await pool.status(settings)
    assert status["free_model_preferences"][-1]["eligible_free_routes"] == []
    assert status["free_model_preferences"][0]["available_free_routes"] == [glm.ref]


@pytest.mark.parametrize(
    "value", ["deepseek-flash", "kimi-k3,kimi-k3", "arbitrary", "glm-5.3-flashx"]
)
def test_reject_ambiguous_or_duplicate_preferences(value):
    with pytest.raises(ValidationError):
        Settings(free_model_priority=value)


@pytest.mark.asyncio
async def test_disabled_preferences_use_provider_order(monkeypatch):
    ds = route("kilo", "deepseek-v4.1-flash:free")
    kimi = route("open_router", "kimi-k3:free")
    pool = pool_with(monkeypatch, ds, kimi)
    settings = Settings(free_model_priority="none")
    assert await pool.select(settings, {}) == (kimi, ds)
    assert (await pool.status(settings))["free_model_preferences"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submitted,expected", [(None, None), ([], "none"), (["kimi-k3"], "kimi-k3")]
)
async def test_policy_omission_preserves_preference_and_empty_explicitly_disables(
    submitted, expected
):
    from types import SimpleNamespace

    from free_claude_code.api.free_pool_routes import (
        RoutingPolicyPayload,
        routing_policy,
    )

    apply = AsyncMock(return_value={})
    services = SimpleNamespace(
        admin=SimpleNamespace(apply_admin_config=apply),
        requests=SimpleNamespace(current_settings=Settings),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                services=services,
                free_pool=SimpleNamespace(status=AsyncMock(return_value={})),
            )
        )
    )
    payload = RoutingPolicyPayload(
        allow_subscriptions=False,
        allow_paid_api=False,
        billing_priority=["free", "subscription", "paid_api"],
        provider_priority=[],
        disabled_providers=[],
        free_model_priority=submitted,
    )
    await routing_policy(payload, request)
    saved = apply.call_args.args[0]
    if expected is None:
        assert "FREE_MODEL_PRIORITY" not in saved
    else:
        assert saved["FREE_MODEL_PRIORITY"] == expected
