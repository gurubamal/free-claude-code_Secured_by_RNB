"""Real inference receipts guide routing without weakening billing/cooldown gates."""

import json
import os

import pytest
from free_helpers import freeze_pool, model

from free_claude_code.application import route_health
from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.fallback_order import prefer_next_provider
from free_claude_code.core.private_storage import read_private_text


@pytest.mark.asyncio
async def test_verified_free_precedes_family_but_never_changes_billing_or_manual_choice(
    monkeypatch,
):
    clock = [1789920000.0]
    monkeypatch.setattr(route_health, "time", lambda: clock[0])
    s = Settings(open_router_api_key="synthetic-route-key", allow_paid_api_models=True)
    preferred = model(name="deepseek/deepseek-v4.1-flash:free")
    verified = model("kilo", "other:free")
    paid = FreeModel("deepseek", "paid", 1048576, 8192, True, False, "paid_api")
    pool = freeze_pool(AutomaticFreePool(), [preferred, verified, paid])
    assert (await pool.select(s, {}))[0] == preferred
    pool.record_success(verified, settings=s)
    pool.record_success(paid, settings=s)
    assert await pool.select(s, {}) == (verified, preferred, paid)
    selected = s.model_copy(
        update={
            "routing_selected_provider": "open_router",
            "routing_selected_model": preferred.model_id,
        }
    )
    assert (await pool.select(selected, {}))[0] == preferred
    clock[0] += route_health.VERIFIED_TTL_SECONDS + 1
    assert pool.model_health(s, verified)["state"] == "STALE"
    assert (await pool.select(s, {}))[0] == preferred


@pytest.mark.asyncio
async def test_failure_excluded_then_rechecked_last_and_success_restores_health(
    monkeypatch,
):
    from free_claude_code.application import free_pool

    clock = [1789920000.0]
    monkeypatch.setattr(route_health, "time", lambda: clock[0])
    monkeypatch.setattr(free_pool, "time", lambda: clock[0])
    s = Settings(open_router_api_key="synthetic-route-key")
    preferred = model(name="deepseek/deepseek-v4.1-flash:free")
    other = model("kilo", "other:free")
    pool = freeze_pool(AutomaticFreePool(), [preferred, other])
    pool.record_success(preferred, settings=s)
    pool.record_failure(
        s, preferred, ExecutionFailure(FailureKind.UPSTREAM, 502, "synthetic", False)
    )
    assert await pool.select(s, {}) == (other,)
    assert pool.model_health(s, preferred)["state"] == "FAILED"
    clock[0] += 46
    assert pool.model_health(s, preferred)["state"] == "RECHECK_DUE"
    assert await pool.select(s, {}) == (other, preferred)
    pool.record_success(preferred, settings=s)
    assert pool.model_health(s, preferred)["state"] == "VERIFIED"
    assert (await pool.select(s, {}))[0] == preferred


def test_receipts_survive_restart_bind_credentials_and_store_no_content():
    s = Settings(open_router_api_key="synthetic-private-credential")
    m = model()
    pool = AutomaticFreePool()
    pool.record_success(m, settings=s)
    restored = AutomaticFreePool()
    assert restored.model_health(s, m)["state"] == "VERIFIED"
    assert (
        restored.model_health(
            s.model_copy(update={"open_router_api_key": "rotated"}), m
        )["state"]
        == "UNTESTED"
    )
    plain = read_private_text(restored._health.path)
    assert s.open_router_api_key not in plain
    records = json.loads(plain)
    assert all(
        set(r) == {"state", "checked_at", "status_code"} for r in records.values()
    )
    if os.name == "nt":
        assert restored._health.path.read_text().startswith("FCC-DPAPI-V1:")


@pytest.mark.parametrize("has_output", [False, True])
def test_only_completed_output_can_be_green_and_local_errors_do_not_mark_provider_failed(
    has_output,
):
    s, m = Settings(), model()
    pool = AutomaticFreePool()
    pool.record_attempt(m)
    assert pool.model_health(s, m)["state"] == "UNTESTED"
    pool.record_success(m, settings=s, has_output=has_output)
    expected = "VERIFIED" if has_output else "UNTESTED"
    assert pool.model_health(s, m)["state"] == expected
    pool.record_failure(
        s,
        m,
        ExecutionFailure(FailureKind.INVALID_REQUEST, 400, "conversion", False),
        affects_availability=False,
    )
    assert pool.model_health(s, m)["state"] == expected


@pytest.mark.asyncio
async def test_status_free_guide_excludes_paid_disabled_and_shared_quota_blocked_routes():
    s = Settings(allow_paid_api_models=True)
    free, sibling = model(name="one:free"), model(name="two:free")
    paid = FreeModel("deepseek", "paid", 1048576, 8192, True, False, "paid_api")
    pool = freeze_pool(AutomaticFreePool(), [free, sibling, paid])
    pool._reports = [
        {"provider": p, "state": "ELIGIBLE", "models": n}
        for p, n in [("open_router", 2), ("deepseek", 1)]
    ]
    for m in (free, paid):
        pool.record_success(m, settings=s)
    data = await pool.status(s)
    assert [r["model"] for r in data["verified_free_routes"]] == ["one:free"]
    assert data["health_ttl_seconds"] == 900
    excluded = s.model_copy(update={"routing_disabled_providers": "open_router"})
    assert (await pool.status(excluded))["verified_free_routes"] == []
    pool.record_failure(
        s, free, ExecutionFailure(FailureKind.RATE_LIMIT, 429, "synthetic quota", False)
    )
    data = await pool.status(s)
    assert data["verified_free_routes"] == []
    row = data["providers"][0]
    assert row["health_state"] == "FAILED" and row["failed_models"] == 2
    assert row["model_details"][1]["health"]["checked_at"] is None
    assert row["model_details"][1]["health"]["state"] == "FAILED"


def test_provider_diversification_never_jumps_billing_category():
    failed, sibling, other = (
        model(name="first"),
        model(name="sibling"),
        model("kilo", "other"),
    )
    paid = FreeModel("deepseek", "paid", 1048576, 8192, True, False, "paid_api")
    candidates = [failed, sibling, paid]
    prefer_next_provider(candidates, 0)
    assert candidates == [failed, sibling, paid]
    candidates = [failed, sibling, other, paid]
    prefer_next_provider(candidates, 0)
    assert candidates == [failed, other, sibling, paid]
    # An exhausted manual paid choice returns to the next automatic category.
    candidates = [paid, failed, other]
    prefer_next_provider(candidates, 0)
    assert candidates == [paid, failed, other]


def test_bad_private_health_file_does_not_prevent_gateway_startup():
    health = route_health.RouteHealth()
    health.path.parent.mkdir(parents=True, exist_ok=True)
    health.path.write_text("invalid JSON", encoding="utf-8")
    assert route_health.RouteHealth().records == {}
