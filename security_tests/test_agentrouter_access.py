"""Client rejection stays distinct from invalid credentials and eligible routes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
from openai import AsyncOpenAI

from free_claude_code.application.free_pool import AutomaticFreePool
from free_claude_code.config.admin.manifest import FIELD_BY_KEY
from free_claude_code.config.settings import Settings
from free_claude_code.core.agentrouter_errors import (
    CLIENT_ACCESS_MESSAGE,
    agentrouter_access_message,
)
from free_claude_code.providers.credential_validation import check_credentials
from free_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from free_claude_code.providers.openai_chat.provider import OpenAIChatProvider
from free_claude_code.providers.runtime.runtime import ProviderRuntime
from free_claude_code.runtime.application import ApplicationRuntime
from free_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.providers.support import immediate_admission, make_provider_config

DENIED = {
    "error": {
        "message": "unauthorized client detected synthetic-private-secret <script>bad</script>",
    },
    "message": "UNAUTHENTICATED",
    "success": False,
    "type": "unauthorized_client_error",
}
BASE = "https://agentrouter.org/v1"
ROW = {
    "id": "synthetic-chat",
    "context_length": 1000000,
    "supported_parameters": ["tools"],
}


@pytest.mark.asyncio
async def test_rejected_catalog_skipped_and_key_check_does_not_blame_key(monkeypatch):
    settings = Settings(
        agentrouter_api_key="synthetic-private-secret",
        inception_api_key="synthetic-fallback-secret",
        allow_paid_api_models=True,
        routing_priority="paid_api,subscription,free",
    )
    denied = True
    calls = []

    def reply(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.host == "models.dev":
            return httpx.Response(200, json={})
        if request.url.host == "agentrouter.org":
            assert str(request.url) == BASE + "/models"
            assert request.headers["Authorization"] == "Bearer synthetic-private-secret"
            return (
                httpx.Response(401, json=DENIED)
                if denied
                else httpx.Response(200, json={"data": [ROW]})
            )
        assert (
            str(request.url)
            == "https://api.inceptionlabs.ai/v1/chat/completions/models"
        )
        return httpx.Response(200, json={"data": [ROW]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(**kw, transport=httpx.MockTransport(reply)),
    )
    checks = await check_credentials(settings, ("AGENTROUTER_API_KEY",))
    assert checks[0].status == "unverified"
    assert checks[0].message == CLIENT_ACCESS_MESSAGE
    assert FIELD_BY_KEY["AGENTROUTER_API_KEY"].secret
    pool = AutomaticFreePool()
    monkeypatch.setattr(pool, "_discover_local", AsyncMock(return_value=([], True)))
    status = await pool.status(settings, force=True)
    report = next(r for r in status["providers"] if r["provider"] == "agentrouter")
    assert report["state"] == "DISCOVERY_REJECTED"
    assert report["reason"] == "client_access_restricted"
    assert report["message"] == CLIENT_ACCESS_MESSAGE
    assert report["health_state"] == "FAILED" and report["models"] == 0
    assert "synthetic-private-secret" not in json.dumps(status)
    assert "<script>" not in json.dumps(status)
    candidates = await pool.select(settings, {})
    assert [m.provider_id for m in candidates] == ["inception"]
    # Provider authorization can recover without replacing the key.
    denied = False
    await pool.refresh(settings, force=True)
    assert {m.provider_id for m in pool._catalog} == {"inception", "agentrouter"}
    checks = await check_credentials(settings, ("AGENTROUTER_API_KEY",))
    assert checks[0].status == "unverified"  # A GET is not an inference test.
    assert "catalog is accessible" in checks[0].message
    assert all(request.method == "GET" for request in calls)


@pytest.mark.asyncio
async def test_real_sdk_failure_reaches_admin_and_clears_after_recovery():
    denied = True

    def reply(request):
        assert str(request.url) == BASE + "/models"
        return (
            httpx2.Response(401, json=DENIED)
            if denied
            else httpx2.Response(200, json={"data": [ROW]})
        )

    settings = Settings(agentrouter_api_key="synthetic", allow_paid_api_models=True)
    async with AsyncOpenAI(
        api_key="synthetic",
        base_url=BASE,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(reply)),
    ) as client:
        provider = OpenAIChatProvider(
            make_provider_config(api_key="synthetic", base_url=BASE),
            admission=immediate_admission(provider_name="agentrouter"),
            profile=OPENAI_CHAT_PROFILES["agentrouter"],
            client=client,
        )

        async def construct(provider_id, settings):
            return provider

        manager = ProviderRuntimeManager(
            settings,
            runtime_factory=lambda s: ProviderRuntime(
                s, provider_constructor=construct
            ),
        )
        try:
            result = await ApplicationRuntime.test_provider(
                SimpleNamespace(provider_manager=manager), "agentrouter"
            )
            assert result == {
                "provider_id": "agentrouter",
                "ok": False,
                "message": CLIENT_ACCESS_MESSAGE,
            }
            status = manager.catalog_status()
            assert status["provider_errors"]["agentrouter"] == CLIENT_ACCESS_MESSAGE
            assert "synthetic-private-secret" not in json.dumps(status)
            assert "<script>" not in json.dumps(status)
            denied = False
            await manager.refresh_provider("agentrouter")
            assert "agentrouter" not in manager.catalog_status()["provider_errors"]
        finally:
            await manager.close()


@pytest.mark.parametrize(
    "payload,status,recognized",
    [
        (DENIED, 401, True),
        ({"error": {"type": "unauthorized_client_error"}}, 403, True),
        ({"error": {"code": "unauthorized_client_error"}}, 401, True),
        ({"message": "unauthorized client detected secret"}, 401, True),
        ({"error": {"message": "Invalid API key secret"}}, 401, False),
        (DENIED, 200, False),
        ({"error": ["secret"], "type": ["unauthorized_client_error"]}, 401, False),
        ("secret", 403, False),
        (None, 401, False),
    ],
)
def test_only_known_client_rejections_get_fixed_diagnostic(payload, status, recognized):
    assert agentrouter_access_message(payload, status) == (
        CLIENT_ACCESS_MESSAGE if recognized else None
    )
