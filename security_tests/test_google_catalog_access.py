"""Google catalog errors stay actionable and free of upstream account metadata."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from free_claude_code.application.free_pool import AutomaticFreePool
from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.config.provider_catalog import GEMINI_DEFAULT_BASE
from free_claude_code.config.settings import Settings
from free_claude_code.core.google_errors import GoogleAccessError, google_access_message
from free_claude_code.providers.credential_validation import check_credentials
from free_claude_code.providers.gemini import GeminiProvider
from free_claude_code.providers.runtime.runtime import ProviderRuntime
from free_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.providers.support import immediate_admission, make_provider_config

SUSPENDED = {
    "error": {
        "message": "private-project-id synthetic-secret",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "domain": "googleapis.com",
                "reason": "CONSUMER_SUSPENDED",
                "metadata": {"consumer": "private-project-id"},
            }
        ],
    }
}


def mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(**kw, transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_native_key_catalog_pagination_and_context(monkeypatch):
    calls = []

    def reply(request):
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == "synthetic-secret"
        assert "key" not in request.url.params
        calls.append(request)
        if request.url.params.get("pageToken") == "next":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-test",
                        "supportedGenerationMethods": ["generateContent"],
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 8192,
                    },
                    {
                        "name": "models/embed",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ],
                "nextPageToken": "next",
            },
        )

    mock_http(monkeypatch, reply)
    provider = GeminiProvider(
        make_provider_config(api_key="synthetic-secret", base_url=GEMINI_DEFAULT_BASE),
        admission=immediate_admission(),
    )
    try:
        infos = await provider.list_model_infos()
        assert infos == frozenset(
            {
                ProviderModelInfo(
                    "gemini-test", context_window_tokens=1048576, max_output_tokens=8192
                )
            }
        )
        assert len(calls) == 2
    finally:
        await provider.cleanup()


@pytest.mark.asyncio
async def test_suspension_visible_in_discovery_validation_and_runtime(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(403, json=SUSPENDED))
    settings = Settings(gemini_api_key="synthetic-secret", allow_paid_api_models=True)
    provider = GeminiProvider(
        make_provider_config(api_key="synthetic-secret", base_url=GEMINI_DEFAULT_BASE),
        admission=immediate_admission(),
    )
    with pytest.raises(GoogleAccessError) as raised:
        await provider.list_model_infos()
    assert "CONSUMER_SUSPENDED" in str(raised.value)
    assert "private-project-id" not in str(raised.value)
    checks = await check_credentials(settings, ("GEMINI_API_KEY",))
    assert checks[0].status == "unverified"
    assert checks[0].message == str(raised.value)
    pool = AutomaticFreePool()
    monkeypatch.setattr(pool, "_discover_local", AsyncMock(return_value=([], True)))
    await pool.refresh(settings)
    report = next(r for r in pool._reports if r["provider"] == "gemini")
    assert report["state"] == "DISCOVERY_REJECTED" and report["message"] == str(
        raised.value
    )

    async def construct(provider_id, settings):
        return provider

    manager = ProviderRuntimeManager(
        settings,
        runtime_factory=lambda s: ProviderRuntime(s, provider_constructor=construct),
    )
    try:
        await manager.refresh_provider("gemini")
        status = manager.catalog_status()
        assert status["provider_errors"]["gemini"] == str(raised.value)
        assert "private-project-id" not in json.dumps(status)
        assert "synthetic-secret" not in json.dumps(status)
        monkeypatch.setattr(
            provider, "list_model_infos", AsyncMock(return_value=frozenset())
        )
        await manager.refresh_provider("gemini")
        assert "gemini" not in manager.catalog_status()["provider_errors"]
    finally:
        await manager.close()


def test_unknown_raw_diagnostics_are_never_returned():
    assert (
        google_access_message({"error": {"message": "CONSUMER_SUSPENDED secret"}})
        is None
    )
    assert (
        google_access_message(
            {
                "error": {
                    "details": [
                        {"reason": "private-secret", "domain": "googleapis.com"}
                    ]
                }
            }
        )
        is None
    )
    with pytest.raises(ValueError):
        GoogleAccessError("private-secret")
