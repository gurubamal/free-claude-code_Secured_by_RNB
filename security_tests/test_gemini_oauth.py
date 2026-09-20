"""Google OAuth lifecycle, real loopback callbacks, SDK bearer auth and routing."""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from free_claude_code.application.connected_accounts import ConnectedAccountLoginMode
from free_claude_code.application.free_pool import AutomaticFreePool
from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.config.provider_catalog import GEMINI_DEFAULT_BASE
from free_claude_code.config.settings import Settings
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
)
from free_claude_code.providers.gemini_oauth.auth import GeminiOAuthManager
from free_claude_code.providers.gemini_oauth.client import (
    MODELS_URL,
    GeminiOAuthProvider,
)
from free_claude_code.providers.gemini_oauth.login import (
    REVOKE_URL,
    SCOPE,
    TOKEN_URL,
    GoogleBrowserLogin,
    GoogleLoginError,
)
from tests.providers.support import immediate_admission, make_provider_config


def configured(**extra):
    return Settings(
        gemini_oauth_client_id="synthetic-client.apps.googleusercontent.com",
        gemini_oauth_client_secret="synthetic-client-secret",
        gemini_oauth_project_id="synthetic-project",
        **extra,
    )


def record(settings, **extra):
    return {
        "version": 1,
        "client_id": settings.gemini_oauth_client_id,
        "client_secret": settings.gemini_oauth_client_secret,
        "project_id": settings.gemini_oauth_project_id,
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "expires_at": int(time.time()) + 3600,
        **extra,
    }


@pytest.mark.asyncio
async def test_real_callback_requires_state_pkce_and_is_single_use():
    flow = await GoogleBrowserLogin.start(configured().gemini_oauth_client_id)
    try:
        query = parse_qs(urlsplit(flow.authorization_url).query)
        assert urlsplit(flow.authorization_url).netloc == "accounts.google.com"
        assert query["scope"] == [SCOPE]
        assert query["code_challenge_method"] == ["S256"]
        assert "secret" not in flow.authorization_url
        assert urlsplit(flow.redirect_uri).hostname == "127.0.0.1"
        async with httpx.AsyncClient(trust_env=False) as client:
            bad = await client.get(
                flow.redirect_uri, params={"state": "wrong", "code": "secret-code"}
            )
            assert bad.status_code == 400 and not flow._result.done()
            hostile = await client.get(
                flow.redirect_uri,
                headers={"Host": "attacker.invalid"},
                params={"state": query["state"][0], "code": "code"},
            )
            assert hostile.status_code == 400 and not flow._result.done()
            duplicate = await client.get(
                flow.redirect_uri,
                params=[
                    ("state", query["state"][0]),
                    ("state", query["state"][0]),
                    ("code", "code"),
                ],
            )
            assert duplicate.status_code == 400 and not flow._result.done()
            response = await client.get(
                flow.redirect_uri,
                params={"state": query["state"][0], "code": "secret-code"},
            )
            assert response.status_code == 200 and "secret-code" not in response.text
            assert response.headers["cache-control"] == "no-store"
            grant = await flow.wait()
            assert grant.code == "secret-code"
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(grant.verifier.encode()).digest()
                )
                .decode()
                .rstrip("=")
            )
            assert query["code_challenge"] == [challenge]
            replay = await client.get(
                flow.redirect_uri, params={"state": query["state"][0], "code": "replay"}
            )
            assert replay.status_code == 409
    finally:
        await flow.close()


@pytest.mark.asyncio
async def test_login_saves_private_credentials_without_claiming_inference(tmp_path):
    requests = []

    def respond(request):
        assert str(request.url) == TOKEN_URL
        fields = parse_qs(request.content.decode())
        assert fields["grant_type"] == ["authorization_code"]
        assert "code_verifier" in fields and fields["client_secret"] == [
            "synthetic-client-secret"
        ]
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": SCOPE,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        path = tmp_path / "auth.json"
        manager = GeminiOAuthManager(configured, credential_path=path, client=client)
        try:
            status = await manager.start_login(ConnectedAccountLoginMode.BROWSER)
            assert status.state == "connecting" and not status.connected
            query = parse_qs(urlsplit(status.authorization_url).query)
            async with httpx.AsyncClient(trust_env=False) as browser:
                response = await browser.get(
                    query["redirect_uri"][0],
                    params={"state": query["state"][0], "code": "synthetic-code"},
                )
                assert response.status_code == 200
            await asyncio.wait_for(manager._task, 3)
            assert manager.status().connected
            assert (
                manager.status().model_count is None
            )  # Sign-in is not catalog/inference evidence.
            safe = json.dumps(manager.status().as_dict())
            assert all(
                secret not in safe
                for secret in (
                    "synthetic-access",
                    "synthetic-refresh",
                    "synthetic-client-secret",
                )
            )
            assert (
                json.loads(read_private_text(path))["refresh_token"]
                == "synthetic-refresh"
            )
            assert len(requests) == 1
        finally:
            await manager.close()


@pytest.mark.asyncio
async def test_cancel_closes_listener_and_cannot_persist(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No token call"))
    ) as client:
        manager = GeminiOAuthManager(
            configured, credential_path=tmp_path / "auth.json", client=client
        )
        status = await manager.start_login(ConnectedAccountLoginMode.BROWSER)
        flow = manager._browser
        assert status.authorization_url
        status = await manager.cancel_login()
        assert not status.connected and status.state == "disconnected"
        assert not manager._path.exists() and manager._browser is None
        assert not flow._runner.addresses
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "revoked", "outage"])
async def test_refresh_is_serialized_and_only_revoked_grants_are_removed(
    tmp_path, outcome
):
    path = tmp_path / "auth.json"
    atomic_write_private_text(path, json.dumps(record(configured(), expires_at=0)))
    calls = []

    async def respond(request):
        calls.append(request)
        assert str(request.url) == TOKEN_URL
        assert parse_qs(request.content.decode())["refresh_token"] == [
            "synthetic-refresh"
        ]
        await asyncio.sleep(0)
        if outcome == "revoked":
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "private-payload"},
            )
        if outcome == "outage":
            return httpx.Response(503, json={"error": "private-payload"})
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-renewed",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        manager = GeminiOAuthManager(configured, credential_path=path, client=client)
        if outcome == "success":
            assert (
                await asyncio.gather(*(manager.access_token() for _ in range(5)))
                == ["synthetic-renewed"] * 5
            )
            assert len(calls) == 1
            assert (
                json.loads(read_private_text(path))["refresh_token"]
                == "synthetic-refresh"
            )
        else:
            with pytest.raises(GoogleLoginError) as error:
                await manager.access_token()
            assert "private-payload" not in str(error.value)
            assert path.exists() == (outcome == "outage")
            assert manager.is_connected() == (outcome == "outage")
        await manager.close()


@pytest.mark.asyncio
async def test_disconnect_removes_tokens_even_when_google_revoke_fails(tmp_path):
    path = tmp_path / "auth.json"
    atomic_write_private_text(path, json.dumps(record(configured())))

    def respond(request):
        assert str(request.url) == REVOKE_URL and not request.url.query
        assert parse_qs(request.content.decode()) == {"token": ["synthetic-refresh"]}
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        manager = GeminiOAuthManager(configured, credential_path=path, client=client)
        status = await manager.disconnect()
        assert not status.connected and not path.exists()
        assert "revocation was not confirmed" in status.message
        with pytest.raises(GoogleLoginError):
            await manager.access_token()
        await manager.close()


@pytest.mark.asyncio
async def test_configuration_change_requires_new_consent(tmp_path):
    path = tmp_path / "auth.json"
    settings = configured()
    atomic_write_private_text(path, json.dumps(record(settings)))
    manager = GeminiOAuthManager(lambda: settings, credential_path=path)
    try:
        assert manager.is_connected()
        with pytest.raises(GoogleLoginError, match="project changed"):
            await manager.access_token(project_id="different-project")
        settings = settings.model_copy(
            update={"gemini_oauth_project_id": "another-project"}
        )
        assert not manager.is_connected()
        with pytest.raises(GoogleLoginError):
            await manager.access_token()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sdk_sends_renewable_oauth_header_and_project_on_each_request():
    import httpx2

    auth = SimpleNamespace(
        access_token=AsyncMock(side_effect=["synthetic-first", "synthetic-second"])
    )
    provider = GeminiOAuthProvider(
        make_provider_config(api_key=None, base_url=GEMINI_DEFAULT_BASE),
        auth=auth,
        project_id="synthetic-project",
        admission=immediate_admission(),
    )
    headers = []

    def respond(request):
        assert str(request.url) == GEMINI_DEFAULT_BASE + "chat/completions"
        assert request.headers["x-goog-user-project"] == "synthetic-project"
        headers.append(request.headers["authorization"])
        return httpx2.Response(
            200,
            json={
                "id": "synthetic-chat",
                "object": "chat.completion",
                "created": 1,
                "model": "gemini-large",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    await provider._client._client.aclose()
    provider._client._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond)
    )
    try:
        for _ in range(2):
            result = await provider._client.chat.completions.create(
                model="gemini-large",
                messages=[{"role": "user", "content": "test"}],
                max_tokens=4,
            )
            assert result.choices[0].message.content == "OK"
        assert headers == ["Bearer synthetic-first", "Bearer synthetic-second"]
    finally:
        await provider.cleanup()


@pytest.mark.parametrize("oauth_failure", [False, True])
def test_chat_ingress_uses_oauth_owner_and_falls_back_before_output(
    monkeypatch, oauth_failure
):
    from free_helpers import freeze_pool
    from starlette.testclient import TestClient

    from free_claude_code.api import free_chat_routes
    from free_claude_code.api.app import create_app
    from free_claude_code.application.free_pool import FreeModel

    settings = configured(
        allow_paid_api_models=True,
        deepseek_api_key="synthetic-api",
        routing_provider_priority="gemini_oauth,deepseek",
    )
    provider = SimpleNamespace(
        authorization_headers=AsyncMock(
            side_effect=GoogleLoginError("Reconnect") if oauth_failure else None,
            return_value={
                "Authorization": "Bearer synthetic-oauth",
                "x-goog-user-project": "synthetic-project",
            },
        )
    )
    lease = SimpleNamespace(
        resolve_provider=AsyncMock(return_value=provider), release=AsyncMock()
    )
    services = SimpleNamespace(
        requests=SimpleNamespace(
            current_settings=lambda: settings, acquire=AsyncMock(return_value=lease)
        ),
        admin=SimpleNamespace(admin_status=None),
    )
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(
        free_chat_routes.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    app = create_app(services)
    freeze_pool(
        app.state.free_pool,
        [
            FreeModel(
                "gemini_oauth", "gemini-large", 1048576, 8192, True, False, "paid_api"
            ),
            FreeModel(
                "deepseek", "deepseek-model", 1048576, 8192, True, False, "paid_api"
            ),
        ],
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={"messages": [{"role": "user", "content": "test"}]},
        )
    assert response.status_code == 200
    lease.release.assert_awaited_once()
    assert len(requests) == 1
    if oauth_failure:
        assert requests[0].url.host == "api.deepseek.com"
        assert requests[0].headers["authorization"] == "Bearer synthetic-api"
        assert "x-goog-user-project" not in requests[0].headers
    else:
        assert requests[0].url.host == "generativelanguage.googleapis.com"
        assert requests[0].headers["authorization"] == "Bearer synthetic-oauth"
        assert requests[0].headers["x-goog-user-project"] == "synthetic-project"
    assert "synthetic-oauth" not in response.text


@pytest.mark.asyncio
async def test_native_catalog_pagination_context_and_bearer_header():
    auth = SimpleNamespace(access_token=AsyncMock(return_value="synthetic-oauth"))
    provider = GeminiOAuthProvider(
        make_provider_config(api_key=None, base_url=GEMINI_DEFAULT_BASE),
        auth=auth,
        project_id="synthetic-project",
        admission=immediate_admission(),
    )
    calls = []

    def respond(request):
        assert str(request.url).startswith(MODELS_URL)
        assert request.headers["authorization"] == "Bearer synthetic-oauth"
        assert request.headers["x-goog-user-project"] == "synthetic-project"
        assert "key" not in request.url.params
        calls.append(request)
        if request.url.params.get("pageToken") == "next":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-small",
                            "supportedGenerationMethods": ["generateContent"],
                            "inputTokenLimit": 128000,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-large",
                        "supportedGenerationMethods": ["generateContent"],
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 8192,
                    },
                    {
                        "name": "models/embedding",
                        "supportedGenerationMethods": ["embedContent"],
                        "inputTokenLimit": 9999999,
                    },
                ],
                "nextPageToken": "next",
            },
        )

    await provider._models_client.aclose()
    provider._models_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        infos = {info.model_id: info for info in await provider.list_model_infos()}
        assert set(infos) == {"gemini-large", "gemini-small"} and len(calls) == 2
        assert infos["gemini-large"].context_window_tokens == 1048576
        assert infos["gemini-small"].context_window_tokens == 128000
        # The actual OpenAI SDK uses the renewable bearer callable for chat too.
        assert provider._client._api_key_provider == provider._access_token
        auth.access_token.assert_awaited_with(project_id="synthetic-project")
        assert (
            provider._client.default_headers["x-goog-user-project"]
            == "synthetic-project"
        )
    finally:
        await provider.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_google_oauth_is_paid_api_not_subscription_and_keeps_context_floor(
    monkeypatch, enabled
):
    catalog = SimpleNamespace(
        identities=AsyncMock(return_value={"gemini_oauth": "synthetic-identity"}),
        discover=AsyncMock(
            return_value=(
                ProviderModelInfo(
                    "large", context_window_tokens=1048576, supports_tools=True
                ),
                ProviderModelInfo(
                    "small", context_window_tokens=272000, supports_tools=True
                ),
                ProviderModelInfo("unknown", supports_tools=True),
            )
        ),
    )
    pool = AutomaticFreePool(subscriptions=catalog)
    monkeypatch.setattr(pool, "_fetch", AsyncMock(return_value=b"{}"))
    settings = configured(
        allow_paid_api_models=enabled, allow_subscription_models=False
    )
    await pool.refresh(settings)
    rows = [m for m in pool._catalog if m.provider_id == "gemini_oauth"]
    if enabled:
        assert [(m.model_id, m.billing) for m in rows] == [("large", "paid_api")]
        chosen = await pool.select(settings, {"_fcc_wire_api": "chat"})
        assert chosen[0].provider_id == "gemini_oauth"
    else:
        assert not rows
        catalog.discover.assert_not_awaited()
