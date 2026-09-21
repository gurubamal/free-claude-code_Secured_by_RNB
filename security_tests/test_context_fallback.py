"""256k admission and preferred larger context preserve routing safeguards."""

import pytest
from free_helpers import freeze_pool

from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.application.model_catalog import read_model_catalog
from free_claude_code.application.ports import ModelCatalogSnapshot
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.harnesses.claude import build_claude_proxy_env


def route(provider, name, context, billing="zero_price"):
    return FreeModel(provider, name, context, 8192, True, False, billing)


@pytest.mark.asyncio
async def test_context_preference_preserves_billing_health_and_manual_order():
    small = route("kilo", "deepseek-v4.1-flash:free", 256000)
    large = route("open_router", "other:free", 1000000)
    paid = route("deepseek", "paid", 1000000, "paid_api")
    settings = Settings(allow_paid_api_models=True)
    pool = freeze_pool(AutomaticFreePool(), [small, large, paid])
    assert await pool.select(settings, {}) == (large, small, paid)
    # A verified working fallback beats an untested larger free route.
    pool.record_success(small, settings=settings)
    assert await pool.select(settings, {}) == (small, large, paid)
    # An eligible explicit selection still precedes automatic preferences.
    selected = settings.model_copy(
        update={
            "routing_selected_provider": "deepseek",
            "routing_selected_model": "paid",
            "routing_selected_billing": "paid_api",
        }
    )
    assert (await pool.select(selected, {}))[0] == paid


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 429, 503])
async def test_failed_large_route_automatically_yields_to_smaller_provider(status):
    large = route("open_router", "large:free", 1000000)
    small = route("kilo", "fallback", 256000)
    settings = Settings()
    pool = freeze_pool(AutomaticFreePool(), [small, large])
    assert await pool.select(settings, {}) == (large, small)
    pool.record_failure(
        settings,
        large,
        ExecutionFailure(
            FailureKind.UPSTREAM, status, "Synthetic capacity failure", False
        ),
    )
    assert await pool.select(settings, {}) == (small,)


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [256000, 260000, 272000, 512000])
async def test_fallback_must_fit_entire_request_without_truncation(context):
    small = route("kilo", "fallback", context)
    large = route("open_router", "large:free", 1000000)
    pool = freeze_pool(AutomaticFreePool(), [small, large])
    payload = {"messages": [{"role": "user", "content": "x" * (2 * context)}]}
    assert await pool.select(Settings(), payload) == (large,)
    pool._catalog = (small,)
    with pytest.raises(ExecutionFailure, match="enough room"):
        await pool.select(Settings(), payload)
    assert len(payload["messages"][0]["content"]) == 2 * context


def test_harness_advertises_fallback_floor_and_overrides_stale_compaction_window():
    catalog = read_model_catalog(ModelCatalogSnapshot(Settings(), ()))
    assert catalog.models[0].context_window_tokens == 256000
    env = build_claude_proxy_env(
        proxy_root_url="http://127.0.0.1:8082",
        auth_token="synthetic",
        base_env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "512000"},
    )
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "256000"
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "60"
