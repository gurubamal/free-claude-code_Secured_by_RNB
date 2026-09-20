import json
from types import SimpleNamespace

import httpx
import pytest
from free_helpers import freeze_pool, model
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.config.settings import Settings


@pytest.mark.parametrize("statuses", [[200], [429, 503, 200], [429, 429, 429]])
def test_chat_enforces_zero_cost_and_bounded_retries(monkeypatch, statuses):
    from free_claude_code.api import free_chat_routes

    requests = []
    closed = []

    class Upstream:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

        async def send(self, request, **kwargs):
            requests.append(request)
            status = statuses[len(requests) - 1]
            return httpx.Response(
                status,
                request=request,
                json={"choices": [{"message": {"content": "synthetic"}}]},
            )

        async def aclose(self):
            closed.append(True)

    async def no_wait(seconds):
        assert seconds in {1, 2}

    monkeypatch.setattr(free_chat_routes.httpx, "AsyncClient", Upstream)
    monkeypatch.setattr(free_chat_routes.asyncio, "sleep", no_wait)
    settings = Settings(
        open_router_api_key="synthetic-key",
        groq_api_key="synthetic-groq",
        gemini_api_key="synthetic-gemini",
    )
    services = SimpleNamespace(
        requests=SimpleNamespace(current_settings=lambda: settings),
        admin=SimpleNamespace(admin_status=None),
    )
    app = create_app(services)
    freeze_pool(
        app.state.free_pool,
        [
            model("open_router", "openrouter/free"),
            model("groq", "synthetic-free"),
            model("gemini", "synthetic-free"),
        ],
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={
                "model": "paid-model",
                "messages": [{"role": "user", "content": "hello"}],
                "models": ["paid-model"],
                "plugins": [{"id": "web"}],
                "provider": {"max_price": {"prompt": 999}},
                "max_tokens": 50000,
            },
        )
    assert response.status_code == statuses[-1]
    assert len(requests) == len(statuses) <= 3
    assert closed
    for request in requests:
        body = json.loads(request.content)
        assert body["model"] in {"openrouter/free", "synthetic-free"}
        assert body["max_tokens"] == 8192
        if request.url.host == "openrouter.ai":
            assert all(value == 0 for value in body["provider"]["max_price"].values())
        assert "models" not in body and "plugins" not in body
