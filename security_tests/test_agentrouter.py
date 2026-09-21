"""AgentRouter admission and real-adapter fallback with synthetic upstreams."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest

from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.config.free_providers import POLICY_BY_ID
from free_claude_code.config.settings import Settings


@pytest.mark.asyncio
async def test_catalog_intersection_and_primary_limits(monkeypatch):
    pool = AutomaticFreePool()
    settings = Settings(agentrouter_api_key="synthetic", allow_paid_api_models=True)
    rows = [{"id": "matched"}, {"id": "unknown"}]
    registry = {
        "agentrouter": {
            "models": {
                name: {"limit": {"context": 1000000}, "tool_call": True}
                for name in ("matched", "not-in-live-catalog")
            }
        },
        # Metadata for another provider must not admit unknown AgentRouter IDs.
        "openrouter": {
            "models": {"unknown": {"limit": {"context": 1000000}, "tool_call": True}}
        },
    }

    async def fetch(client, url, **kwargs):
        assert url == "https://agentrouter.org/v1/models"
        assert kwargs["key"] == "synthetic"
        return json.dumps({"data": rows}).encode()

    monkeypatch.setattr(pool, "_fetch", fetch)

    async def discover():
        return await pool._discover(
            None, settings, POLICY_BY_ID["agentrouter"], registry
        )

    models, complete = await discover()
    assert complete and [m.model_id for m in models] == ["matched"]
    assert models[0].context == 1000000 and models[0].billing == "paid_api"
    rows[0]["context_length"] = 128000
    models, _ = await discover()
    assert models[0].context == 128000  # Primary limit beats a larger registry value.
    rows[0]["capabilities"] = {"tools": False}
    models, _ = await discover()
    assert not models


@pytest.mark.parametrize("status", [402, 503])
@pytest.mark.parametrize("wire", ["messages", "responses"])
def test_manual_agentrouter_failure_rotates_without_user_retry(
    monkeypatch, status, wire
):
    from free_helpers import freeze_pool
    from starlette.testclient import TestClient

    from free_claude_code.api.app import create_app
    from free_claude_code.providers.runtime.factory import prepare_provider
    from tests.providers.support import SDKStreamDouble

    settings = Settings(
        agentrouter_api_key="synthetic",
        inception_api_key="synthetic",
        allow_paid_api_models=True,
        routing_priority="paid_api,subscription,free",
        routing_selected_provider="agentrouter",
        routing_selected_model="synthetic-selected",
        routing_selected_billing="paid_api",
    )
    providers = {
        name: prepare_provider(name, {})(settings)
        for name in ("agentrouter", "inception")
    }
    sent = []

    async def fail(**body):
        sent.append(("agentrouter", body["model"]))
        raise openai.APIStatusError(
            "Insufficient credits" if status == 402 else "Service unavailable",
            response=httpx2.Response(
                status,
                request=httpx2.Request(
                    "POST", "https://agentrouter.org/v1/chat/completions"
                ),
            ),
            body={
                "error": {
                    "message": "Insufficient credits"
                    if status == 402
                    else "Service unavailable"
                }
            },
        )

    async def chunks():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="FALLBACK_OK", reasoning_content=None, tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    async def succeed(**body):
        sent.append(("inception", body["model"]))
        return SDKStreamDouble(chunks())

    monkeypatch.setattr(
        providers["agentrouter"]._client.chat.completions, "create", fail
    )
    monkeypatch.setattr(
        providers["inception"]._client.chat.completions, "create", succeed
    )

    async def resolve(name):
        return providers[name]

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
        (
            FreeModel(
                "agentrouter",
                "synthetic-selected",
                1000000,
                8192,
                True,
                False,
                "paid_api",
            ),
            FreeModel(
                "inception", "synthetic-fallback", 260000, 8192, True, False, "paid_api"
            ),
        ),
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
        for provider in providers.values():
            client.portal.call(provider.cleanup)
    assert result.status_code == 200, result.text
    assert "FALLBACK_OK" in result.text
    assert sent == [
        ("agentrouter", "synthetic-selected"),
        ("inception", "synthetic-fallback"),
    ]
    assert app.state.free_pool._last_success == "inception/synthetic-fallback"
