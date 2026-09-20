"""Local CLI/Admin route selection with mandatory automatic fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.cli import route_control
from free_claude_code.config.admin.persistence import prepare_admin_update
from free_claude_code.config.loader import ManagedConfigStore
from free_claude_code.config.settings import Settings
from free_claude_code.core.admin_accounts import AdminAccounts
from free_claude_code.core.failures import ExecutionFailure, FailureKind


def model(provider, name, billing="zero_price", context=1048576, vision=False):
    return FreeModel(provider, name, context, 8192, True, vision, billing)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["messages", "responses", "chat"])
@pytest.mark.parametrize("code", [429, 503])
async def test_manual_preference_fails_into_automatic_and_next_requests_skip_cooldown(
    monkeypatch, wire, code
):
    pool = AutomaticFreePool()
    pool.refresh = AsyncMock()
    preferred = model("kilo", "chosen")
    deepseek = model("open_router", "deepseek-v4.1-flash:free")
    pool._catalog = (deepseek, preferred)
    settings = Settings(
        routing_selected_provider="kilo", routing_selected_model="chosen"
    )
    request = {"_fcc_wire_api": wire}
    assert await pool.select(settings, request) == (preferred, deepseek)
    pool.record_failure(
        settings,
        preferred,
        ExecutionFailure(
            FailureKind.RATE_LIMIT if code == 429 else FailureKind.UPSTREAM,
            code,
            "unavailable",
            False,
        ),
    )
    assert await pool.select(settings, request) == (deepseek,)
    status = await pool.status(settings)
    assert status["selection"]["fallback"] is True
    assert status["selection"]["available_models"] == 0


@pytest.mark.asyncio
async def test_provider_only_selection_and_request_capability_fallback():
    pool = AutomaticFreePool()
    pool.refresh = AsyncMock()
    text = model("kilo", "first")
    visual = model("open_router", "visual:free", vision=True)
    pool._catalog = (text, visual)
    settings = Settings(routing_selected_provider="kilo")
    assert (await pool.select(settings, {}))[0] == text
    assert await pool.select(
        settings, {"messages": [{"content": [{"type": "image_url"}]}]}
    ) == (visual,)
    settings = Settings(
        routing_selected_provider="kilo", routing_disabled_providers="kilo"
    )
    assert await pool.select(settings, {}) == (visual,)


@pytest.mark.asyncio
async def test_selected_paid_never_waives_opt_in_or_context_and_budget_preserves_fallback():
    pool = AutomaticFreePool()
    pool.refresh = AsyncMock()
    paid = model("deepseek", "paid", "paid_api")
    subscription = model("openai", "large", "subscription")
    free = [model("kilo", f"free-{i}") for i in range(20)]
    pool._catalog = (*free, paid, subscription)
    selected = Settings(
        routing_selected_provider="deepseek", routing_selected_billing="paid_api"
    )
    assert paid not in await pool.select(selected, {})
    settings = Settings(
        routing_selected_provider="kilo",
        allow_paid_api_models=True,
        allow_subscription_models=True,
    )
    routes = await pool.select(settings, {})
    assert len(routes) == 12 and routes[-2:] == (subscription, paid)
    pool._catalog = (model("kilo", "small", context=128000), paid)
    assert await pool.select(settings, {}) == (paid,)


def app_with_storage():
    store = ManagedConfigStore()
    store.initialize(env={})
    snapshot = store.read(env={})
    values = dict(snapshot.managed)
    values["OPENROUTER_API_KEY"] = "synthetic-key-preserved"
    store.commit(values)
    settings = store.read(env={}).settings

    async def apply(updates):
        nonlocal settings
        prepared = prepare_admin_update(updates, store.read(env={}), settings)
        if prepared.valid:
            store.commit(prepared.target_values)
            settings = store.read(env={}).settings
        return prepared.applied_response()

    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(current_settings=lambda: settings),
            admin=SimpleNamespace(admin_status=None, apply_admin_config=apply),
        )
    )
    app.state.free_pool.refresh = AsyncMock()
    app.state.free_pool._catalog = (
        model("open_router", "free-choice:free"),
        model("deepseek", "paid", "paid_api"),
        model("kilo", "small", context=128000),
    )
    return app, store, settings


def test_cli_api_authorization_persistence_restore_auto_and_credential_preservation():
    app, store, settings = app_with_storage()
    token = settings.proxy_auth_token
    headers = {"Authorization": "Bearer " + token, "X-FCC-Route-Control": "1"}
    body = {"mode": "selected", "provider": "open_router", "model": "free-choice:free"}
    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 4321)
    ) as client:
        assert client.post("/v1/routing/selection", json=body).status_code == 401
        assert (
            client.post(
                "/v1/routing/selection",
                json=body,
                headers={"Authorization": "Bearer " + token},
            ).status_code
            == 403
        )
        for origin in ["http://127.0.0.1", "https://evil.example"]:
            assert (
                client.post(
                    "/v1/routing/selection",
                    json=body,
                    headers={**headers, "Origin": origin},
                ).status_code
                == 403
            )
        response = client.post("/v1/routing/selection", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["selection"]["model"] == "free-choice:free"
        assert (
            token not in response.text
            and "synthetic-key-preserved" not in response.text
        )
        # Fresh read simulates another CLI process / daemon restart.
        assert store.read(env={}).settings.routing_selected_model == "free-choice:free"
        assert (
            store.read(env={}).settings.open_router_api_key == "synthetic-key-preserved"
        )
        assert (
            client.post(
                "/v1/routing/selection", json={"mode": "automatic"}, headers=headers
            ).status_code
            == 200
        )
        assert store.read(env={}).settings.routing_selected_provider is None
        assert store.read(env={}).settings.auto_free_models is True


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "selected", "provider": "deepseek", "model": "paid"},
        {
            "mode": "selected",
            "provider": "deepseek",
            "model": "paid",
            "billing": "paid_api",
        },
        {"mode": "selected", "provider": "kilo", "model": "small"},
        {"mode": "selected", "provider": "open_router", "model": "made-up"},
    ],
)
def test_api_rejects_unverified_route_or_disabled_billing_without_mutation(body):
    app, store, settings = app_with_storage()
    before = dict(store.read(env={}).managed)
    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 4321)
    ) as client:
        response = client.post(
            "/v1/routing/selection",
            json=body,
            headers={
                "Authorization": "Bearer " + settings.proxy_auth_token,
                "X-FCC-Route-Control": "1",
            },
        )
        assert response.status_code == 400
    assert dict(store.read(env={}).managed) == before


def test_remote_cli_blocked_and_web_requires_session_and_csrf():
    app, _, settings = app_with_storage()
    body = {"mode": "automatic"}
    with TestClient(
        app, base_url="http://127.0.0.1", client=("203.0.113.9", 4321)
    ) as client:
        assert (
            client.post(
                "/v1/routing/selection",
                json=body,
                headers={
                    "Authorization": "Bearer " + settings.proxy_auth_token,
                    "X-FCC-Route-Control": "1",
                },
            ).status_code
            == 403
        )
    password = "Synthetic Selection River 492!"
    AdminAccounts().reset_password(password)
    with TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 4321)
    ) as client:
        assert (
            client.post(
                "/admin/api/free/selection", json=body, headers={"X-FCC-Admin": "1"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/admin/auth/login",
                json={"username": "admin", "password": password},
                headers={"X-FCC-Admin": "1"},
            ).status_code
            == 200
        )
        assert client.post("/admin/api/free/selection", json=body).status_code == 403
        assert (
            client.post(
                "/admin/api/free/selection", json=body, headers={"X-FCC-Admin": "1"}
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    "argv,method,body",
    [
        (["status"], "GET", None),
        (["list", "--provider", "open_router"], "GET", None),
        (
            ["use", "open_router", "free-choice:free"],
            "POST",
            {
                "mode": "selected",
                "provider": "open_router",
                "model": "free-choice:free",
                "billing": "free",
            },
        ),
        (
            ["use", "open_router"],
            "POST",
            {
                "mode": "selected",
                "provider": "open_router",
                "model": None,
                "billing": "free",
            },
        ),
        (["auto"], "POST", {"mode": "automatic"}),
    ],
)
def test_cli_commands_use_private_token_without_printing_it(
    monkeypatch, capsys, argv, method, body
):
    import json

    settings = Settings(proxy_auth_token="synthetic-private-cli-token-long-enough")
    monkeypatch.setattr(
        route_control,
        "ManagedConfigStore",
        lambda: SimpleNamespace(read=lambda **_: SimpleNamespace(settings=settings)),
    )

    def handler(request):
        assert request.method == method
        assert request.headers["authorization"] == "Bearer " + settings.proxy_auth_token
        if body:
            assert json.loads(request.content) == body
        return httpx.Response(
            200, json={"providers": [], "selection": {"mode": "automatic"}}
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        route_control.httpx,
        "Client",
        lambda **kw: client_type(**kw, transport=httpx.MockTransport(handler)),
    )
    assert route_control.main(argv) == 0
    output = capsys.readouterr()
    assert settings.proxy_auth_token not in output.out + output.err
