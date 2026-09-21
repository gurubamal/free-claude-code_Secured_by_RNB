"""Primary catalog shapes, wire compatibility and strict billing/context admission."""

import json
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI

from free_claude_code.application.free_pool import AutomaticFreePool
from free_claude_code.config.admin.manifest import FIELD_BY_KEY
from free_claude_code.config.free_providers import POLICY_BY_ID
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.openai_responses import OpenAIResponsesRequest
from free_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from free_claude_code.providers.credential_validation import check_credentials
from free_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from free_claude_code.providers.openai_chat.provider import OpenAIChatProvider
from tests.providers.request_factory import make_messages_request
from tests.providers.support import (
    immediate_admission,
    make_provider_config,
)

# Minimal primary evidence, checked 2026-09-20. No credentials/account payloads.
ROWS = {
    # Synthetic compatibility fixture, not an authenticated AgentRouter catalog.
    "agentrouter": {
        "id": "synthetic-chat",
        "context_length": 1000000,
        "max_output_tokens": 8192,
        "supported_parameters": ["tools"],
        "input_modalities": ["text"],
    },
    "atria": {"id": "Atria-Dawn-Preview"},
    "inception": {
        "id": "mercury-2.5",
        "context_length": 260000,
        "max_output_length": 65536,
        "supported_features": ["tools", "json_mode"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ROWS)
@pytest.mark.parametrize("wire", ["messages", "responses"])
async def test_provider_catalog_and_stream_over_real_adapter(provider_id, wire):
    descriptor = PROVIDER_CATALOG[provider_id]
    row = ROWS[provider_id]
    calls = []

    def respond(request):
        assert request.headers["Authorization"] == "Bearer synthetic-provider-secret"
        assert str(request.url).startswith(descriptor.default_base_url + "/")
        calls.append(request)
        if request.method == "GET":
            suffix = (
                "/chat/completions/models" if provider_id == "inception" else "/models"
            )
            assert str(request.url) == descriptor.default_base_url + suffix
            return httpx2.Response(200, json={"data": [row]})
        assert str(request.url) == descriptor.default_base_url + "/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == row["id"] and body["stream"] is True
        assert "diffusing" not in body and "extra_body" not in body
        if provider_id == "inception":
            assert body["reasoning_effort"] == "high"
            assert body["max_completion_tokens"] > 0
            assert "max_tokens" not in body and "top_p" not in body
            assert "parallel_tool_calls" not in body
        chunk = {
            "id": "test-chat",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": row["id"],
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Synthetic provider answer"},
                    "finish_reason": "stop",
                }
            ],
        }
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    async with AsyncOpenAI(
        api_key="synthetic-provider-secret",
        base_url=descriptor.default_base_url,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        provider = OpenAIChatProvider(
            make_provider_config(
                api_key="synthetic-provider-secret",
                base_url=descriptor.default_base_url,
            ),
            admission=immediate_admission(provider_name=provider_id),
            profile=OPENAI_CHAT_PROFILES[provider_id],
            client=client,
        )
        infos = await provider.list_model_infos()
        assert len(infos) == 1
        assert next(iter(infos)).context_window_tokens == {
            "atria": 256000,
            "inception": 260000,
            "agentrouter": 1000000,
        }[provider_id]
        reasoning = ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
        stream = (
            provider.stream_messages(
                make_messages_request(row["id"]), reasoning=reasoning
            )
            if wire == "messages"
            else provider.stream_responses(
                OpenAIResponsesRequest(
                    model=row["id"], input="hello", max_output_tokens=100
                ),
                reasoning=reasoning,
            )
        )
        assert "Synthetic provider answer" in "".join([event async for event in stream])
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ROWS)
async def test_paid_opt_in_and_context_floor_preserved(monkeypatch, provider_id):
    pool = AutomaticFreePool()
    row = ROWS[provider_id]
    descriptor = PROVIDER_CATALOG[provider_id]
    settings = Settings(
        **{descriptor.credential_attr: "synthetic"}, allow_paid_api_models=True
    )

    async def fetch(client, url, **kwargs):
        if url == "https://models.dev/api.json":
            return b"{}"
        expected = descriptor.default_base_url + (
            "/chat/completions/models" if provider_id == "inception" else "/models"
        )
        assert url == expected and kwargs["key"] == "synthetic"
        return json.dumps({"data": [row]}).encode()

    monkeypatch.setattr(pool, "_fetch", fetch)
    monkeypatch.setattr(pool, "_discover_local", AsyncMock(return_value=([], True)))
    await pool.refresh(settings, force=True)
    assert len(pool._catalog) == 1 and pool._catalog[0].billing == "paid_api"
    report = next(r for r in pool._reports if r["provider"] == provider_id)
    assert report["state"] == "ELIGIBLE"
    assert report["below_context_minimum"] == 0
    # Smaller entries still fail the floor even with paid permission enabled.
    row = {
        "id": "synthetic-future-chat",
        "context_length": 255999,
        "max_output_length": 8192,
        "supported_features": ["tools"],
    }
    await pool.refresh(settings, force=True)
    assert not pool._catalog
    await pool.refresh(
        settings.model_copy(update={"allow_paid_api_models": False}), force=True
    )
    assert not pool._catalog
    assert (
        next(r for r in pool._reports if r["provider"] == provider_id)["state"]
        == "DISABLED"
    )


@pytest.mark.asyncio
async def test_unknown_atria_version_is_not_given_documented_context(monkeypatch):
    pool = AutomaticFreePool()
    monkeypatch.setattr(
        pool, "_fetch", AsyncMock(return_value=b'{"data":[{"id":"Atria-Dawn-Future"}]}')
    )
    models, _ = await pool._discover(
        None,
        Settings(atria_api_key="synthetic", allow_paid_api_models=True),
        POLICY_BY_ID["atria"],
        {},
    )
    assert not models


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ROWS)
async def test_key_fields_are_secret_and_catalog_is_not_authentication(provider_id):
    descriptor = PROVIDER_CATALOG[provider_id]
    field = FIELD_BY_KEY[descriptor.credential_env]
    assert field.secret and field.settings_attr == descriptor.credential_attr
    checks = await check_credentials(
        Settings(**{descriptor.credential_attr: "synthetic"}),
        (descriptor.credential_env,),
    )
    assert len(checks) == 1 and checks[0].status == "unverified"


@pytest.mark.parametrize(
    "effort,expected", [(None, "instant"), (ReasoningEffort.MAX, "high")]
)
def test_inception_maps_reasoning_to_documented_values(effort, expected):
    body = {}
    policy = (
        ReasoningPolicy.off() if effort is None else ReasoningPolicy.on(effort=effort)
    )
    OPENAI_CHAT_PROFILES["inception"].apply_reasoning_to_body(body, policy)
    assert body == {"reasoning_effort": expected}
