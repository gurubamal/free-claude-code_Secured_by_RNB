"""Resume conversion must preserve instructions/tool history and route availability."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from free_helpers import freeze_pool, model
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.config.settings import Settings
from free_claude_code.providers.runtime.factory import prepare_provider
from tests.providers.support import SDKStreamDouble


def configured_app(monkeypatch):
    settings = Settings(deepseek_api_key="synthetic", allow_paid_api_models=True)
    provider = prepare_provider("deepseek", {})(settings)
    sent = []

    async def chunks():
        for content, finish in [("RESUME_OK", None), (None, "stop")]:
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
        sent.append(body)
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
    candidate = model("deepseek", "deepseek-synthetic")
    freeze_pool(app.state.free_pool, (candidate,))
    return app, settings, provider, sent, candidate


@pytest.mark.parametrize("stream", [False, True])
def test_resume_with_empty_system_reaches_provider_and_preserves_tool_result(
    monkeypatch, stream
):
    app, settings, provider, sent, _ = configured_app(monkeypatch)
    payload = {
        "model": "automatic",
        "max_tokens": 128,
        "stream": stream,
        "thinking": {"type": "disabled"},
        "system": "Preserve these session instructions.",
        "messages": [
            {"role": "user", "content": "Read the saved note."},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_read",
                        "name": "Read",
                        "input": {"file_path": "note.txt"},
                    }
                ],
            },
            {"role": "system", "content": []},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_read",
                        "content": "Synthetic saved result.",
                    }
                ],
            },
            {"role": "system", "content": []},
            {"role": "user", "content": "continue"},
        ],
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json=payload,
        )
        client.portal.call(provider.cleanup)
    assert response.status_code == 200, response.text
    assert "RESUME_OK" in response.text
    assert len(sent) == 1
    assert sent[0]["messages"][0] == {"role": "system", "content": payload["system"]}
    assert any(
        m.get("tool_call_id") == "call_read"
        and m["content"] == "Synthetic saved result."
        for m in sent[0]["messages"]
    )
    assert sent[0]["messages"][-1] == {"role": "user", "content": "continue"}
    assert app.state.free_pool._last_success == "deepseek/deepseek-synthetic"


def test_local_conversion_error_is_400_and_does_not_cool_working_provider(monkeypatch):
    app, settings, provider, sent, candidate = configured_app(monkeypatch)
    invalid = {
        "model": "automatic",
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Synthetic unsupported instruction block",
                    }
                ],
            }
        ],
    }
    headers = {"Authorization": "Bearer " + settings.proxy_auth_token}
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=headers, json=invalid)
        assert response.status_code == 400, response.text
        assert not sent
        assert not app.state.free_pool.cooldown(settings, candidate)
        valid = {
            "model": "automatic",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        response = client.post("/v1/messages", headers=headers, json=valid)
        client.portal.call(provider.cleanup)
    assert response.status_code == 200 and "RESUME_OK" in response.text
    assert len(sent) == 1
