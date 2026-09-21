"""Connected requests recover automatically, without replay or quota resets."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from free_helpers import freeze_pool
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from free_claude_code.api.app import create_app
from free_claude_code.api.capacity_recovery import CapacityRecovery
from free_claude_code.api.request_lifetime import ClientRequestLifetimeMiddleware
from free_claude_code.api.response_streams import (
    ManagedStreamingResponse,
    terminal_execution_error_response,
)
from free_claude_code.application.execution import ProviderExecutor
from free_claude_code.application.free_pool import AutomaticFreePool, FreeModel
from free_claude_code.application.route_budget import (
    RouteAttemptBudget,
    current_route_budget,
    use_route_budget,
)
from free_claude_code.application.routing import ModelRouter
from free_claude_code.config.free_providers import POLICY_BY_ID
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic import MessagesRequest
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.providers.direct_chat import prepare_chat_body

SCOPE = {
    "type": "http",
    "asgi": {"spec_version": "2.4"},
    "method": "POST",
    "path": "/v1/messages",
}


def failure(status=402, *, safe=True):
    return terminal_execution_error_response(
        status_code=status,
        content={
            "type": "error",
            "error": {
                "type": "overloaded_error",
                "message": "synthetic capacity failure",
            },
        },
        recovery_safe=safe,
    )


def recovery(**kwargs):
    settings = Settings()
    pool = SimpleNamespace(refresh=AsyncMock())
    return CapacityRecovery(
        pool,
        lambda: settings,
        wait_seconds=kwargs.pop("wait_seconds", 0.2),
        check_seconds=kwargs.pop("check_seconds", 0.002),
        heartbeat_seconds=0.001,
        **kwargs,
    )


async def receive_forever():
    await asyncio.Event().wait()


async def collect(response):
    messages = []

    async def send(message):
        messages.append(message)

    await response(SCOPE, receive_forever, send)
    return messages


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["messages", "responses", "chat"])
@pytest.mark.parametrize("stream", [False, True])
async def test_waits_then_resumes_once_and_closes_success(wire, stream):
    owner = recovery()
    closed = AsyncMock()

    async def body():
        yield b'data: {"answer":"PRESERVED"}\n\n'

    success = (
        ManagedStreamingResponse(body())
        if stream
        else JSONResponse({"answer": "PRESERVED"})
    )
    if stream:
        success.bind_release(closed)
    create = AsyncMock(side_effect=[failure(), failure(429), success])
    result = await collect(await owner.respond(create, stream=stream, wire=wire))
    assert create.await_count == 3
    assert owner.pool.refresh.await_count == 1
    assert owner.waiting == 0
    assert len([m for m in result if m["type"] == "http.response.start"]) == 1
    text = b"".join(m.get("body", b"") for m in result)
    assert text.count(b"PRESERVED") == 1
    assert b"synthetic capacity failure" not in text
    if stream:
        assert b": FCC waiting" in text
        closed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,safe", [(400, True), (401, True), (403, True), (402, False), (503, False)]
)
async def test_only_explicit_pre_output_failures_can_wait(status, safe):
    owner = recovery()
    original = failure(status, safe=safe)
    create = AsyncMock(return_value=original)
    assert await owner.respond(create, stream=True, wire="messages") is original
    create.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["messages", "responses", "chat"])
async def test_bounded_wait_emits_protocol_error_and_no_retry_storm(wire):
    owner = recovery(wait_seconds=0.01, check_seconds=1)
    create = AsyncMock(return_value=failure())
    messages = await collect(await owner.respond(create, stream=True, wire=wire))
    body = b"".join(m.get("body", b"") for m in messages)
    assert (
        b"waiting limit" in body
        and b"No credits or spending limits were changed" in body
    )
    assert (b"event: error" in body) == (wire != "chat")
    frame = next(
        line.removeprefix(b"data: ")
        for line in body.splitlines()
        if line.startswith(b"data: ")
    )
    payload = json.loads(frame)
    if wire == "responses":
        assert payload["type"] == "error" and payload["code"] == "overloaded_error"
    else:
        assert payload["error"]["type"] == "overloaded_error"
    assert messages[-1].get("more_body", False) is False
    assert owner.waiting == 0
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_nonstream_timeout_returns_503_and_limit_rejects_extra_waiters():
    owner = recovery(wait_seconds=0.001, check_seconds=1)
    create = AsyncMock(return_value=failure())
    messages = await collect(await owner.respond(create, stream=False, wire="messages"))
    assert messages[0]["status"] == 503
    owner.waiting = owner.max_waiting
    messages = await collect(await owner.respond(create, stream=True, wire="messages"))
    assert messages[0]["status"] == 402
    assert owner.waiting == owner.max_waiting


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/v1/messages", "/v1/responses", "/v1/chat/completions"]
)
async def test_client_disconnect_cancels_wait_and_releases_slot(path):
    owner = recovery(check_seconds=10, wait_seconds=30)
    create = AsyncMock(return_value=failure())
    response = await owner.respond(create, stream=True, wire="messages")
    waiting = asyncio.Event()

    async def send(message):
        if b": FCC waiting" in message.get("body", b""):
            waiting.set()

    async def receive():
        await waiting.wait()
        return {"type": "http.disconnect"}

    app = ClientRequestLifetimeMiddleware(response)
    await asyncio.wait_for(app({**SCOPE, "path": path}, receive, send), timeout=1)
    assert owner.waiting == 0
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_during_new_attempt_releases_resources():
    owner = recovery()
    started, released = asyncio.Event(), asyncio.Event()

    async def attempt():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    calls = 0

    async def create():
        nonlocal calls
        calls += 1
        return failure() if calls == 1 else await attempt()

    task = asyncio.create_task(
        collect(await owner.respond(create, stream=True, wire="messages"))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set() and owner.waiting == 0


@pytest.mark.asyncio
async def test_shared_refresh_and_settings_change_stop_waiting():
    owner = recovery()
    await asyncio.gather(*(owner.refresh() for _ in range(8)))
    owner.pool.refresh.assert_awaited_once()
    response = await owner.respond(
        AsyncMock(return_value=failure()), stream=False, wire="messages"
    )
    owner.settings().auto_free_models = False
    messages = await collect(response)
    assert messages[0]["status"] == 402 and owner.waiting == 0


def model():
    return FreeModel(
        "deepseek", "synthetic-large", 1048576, 8192, True, False, "paid_api"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [False, True])
async def test_executor_marks_only_pre_output_exhaustion_for_recovery(output):
    settings = Settings(allow_paid_api_models=True)
    pool = freeze_pool(AutomaticFreePool(), [model()])

    async def stream(*args, **kwargs):
        if output:
            yield 'data: {"type":"content_block_delta","delta":{"text":"ALREADY_STARTED"}}\n\n'
        raise ExecutionFailure(
            FailureKind.PERMISSION, 402, "synthetic billing failure", False
        )

    async def resolve(_):
        return SimpleNamespace(stream_messages=stream)

    executor = ProviderExecutor(resolve, progress_timeout_seconds=1)
    executor.configure_free_routing(pool, settings, [model()])
    request = MessagesRequest(
        model="auto",
        max_tokens=1024,
        messages=[{"role": "user", "content": "synthetic"}],
    )
    routed = ModelRouter(
        settings, free_targets=(model().ref,)
    ).resolve_messages_request(request)
    with pytest.raises(ExecutionFailure) as caught:
        async for _ in executor.stream_messages(
            routed, raw_log_payload={}, request_id="synthetic"
        ):
            pass
    assert caught.value.recovery_safe is (not output)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False, None, "true"])
async def test_deepseek_only_clears_billing_hold_on_explicit_available(available):
    settings = Settings(allow_paid_api_models=True, deepseek_api_key="synthetic")
    pool = AutomaticFreePool()
    pool.record_failure(
        settings,
        model(),
        ExecutionFailure(FailureKind.PERMISSION, 402, "synthetic", False),
    )

    async def fetch(client, url, **kwargs):
        return json.dumps(
            {"is_available": available}
            if url.endswith("/user/balance")
            else {"data": []}
        ).encode()

    pool._fetch = fetch
    await pool._discover(None, settings, POLICY_BY_ID["deepseek"], {})
    assert (pool.cooldown(settings, model()) is None) is (available is True)


@pytest.mark.parametrize("stream", [False, True])
def test_chat_wait_refresh_and_resume_preserves_entire_request(monkeypatch, stream):
    from free_claude_code.api import free_chat_routes

    settings = Settings(allow_paid_api_models=True, deepseek_api_key="synthetic")
    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(current_settings=lambda: settings),
            admin=SimpleNamespace(admin_status=None),
            prepare_chat_body=prepare_chat_body,
        )
    )
    pool = freeze_pool(app.state.free_pool, [model()])
    seen = []

    async def refresh(settings, *, force=False):
        if force:
            # Simulate an authoritative positive balance check, not a quota reset.
            pool._cooldowns.clear()

    pool.refresh = refresh
    owner = app.state.capacity_recovery
    owner.wait_seconds, owner.check_seconds, owner.heartbeat_seconds = 1, 0.002, 0.001

    def serve(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(402, json={"error": "synthetic"})
        return (
            httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"ANSWER"}}]}\n\ndata: [DONE]\n\n',
            )
            if stream
            else httpx.Response(
                200, json={"choices": [{"message": {"content": "ANSWER"}}]}
            )
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        free_chat_routes.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={
                "messages": [{"role": "user", "content": "KEEP_ENTIRE_CONTEXT" * 200}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                "stream": stream,
            },
        )
    assert response.status_code == 200 and "ANSWER" in response.text
    assert len(seen) == 2 and seen[0] == seen[1]
    assert owner.waiting == 0 and pool._last_success == model().ref


@pytest.mark.parametrize(
    "wire,stream", [("messages", True), ("messages", False), ("responses", True)]
)
def test_real_handlers_reacquire_lease_and_resume_after_402(wire, stream):
    settings = Settings(allow_paid_api_models=True, deepseek_api_key="synthetic")
    seen, leases = [], []

    async def upstream(request, **kwargs):
        seen.append(request.model_dump())
        if len(seen) == 1:
            raise ExecutionFailure(
                FailureKind.PERMISSION, 402, "synthetic balance exhausted", False
            )
        events = (
            [
                {
                    "type": "response.completed",
                    "response": {
                        "id": "synthetic",
                        "status": "completed",
                        "output": [],
                    },
                }
            ]
            if wire == "responses"
            else [
                {
                    "type": "message_start",
                    "message": {
                        "id": "synthetic",
                        "type": "message",
                        "role": "assistant",
                        "model": "auto",
                        "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ANSWER"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
        )
        for event in events:
            yield "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"

    async def resolve(_):
        return SimpleNamespace(stream_messages=upstream, stream_responses=upstream)

    async def acquire():
        lease = SimpleNamespace(
            settings=settings,
            generation_id=len(leases) + 1,
            wait_for_token_estimation=AsyncMock(),
            release=AsyncMock(),
            model_info=lambda *args: None,
            is_provider_cached=lambda _: True,
            resolve_provider=resolve,
        )
        leases.append(lease)
        return lease

    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(
                current_settings=lambda: settings, acquire=acquire
            ),
            admin=SimpleNamespace(admin_status=None),
            web_tools=SimpleNamespace(),
        )
    )
    pool = freeze_pool(app.state.free_pool, [model()])

    async def refresh(settings, *, force=False):
        if force:
            assert leases[0].release.await_count == 1
            pool._cooldowns.clear()

    pool.refresh = refresh
    owner = app.state.capacity_recovery
    owner.wait_seconds, owner.check_seconds, owner.heartbeat_seconds = 1, 0.002, 0.001
    payload = {"model": "auto", "stream": stream}
    if wire == "messages":
        payload.update(
            messages=[{"role": "user", "content": "Preserve instructions"}],
            max_tokens=1024,
        )
    else:
        payload["input"] = "Preserve instructions"
    with TestClient(app) as client:
        result = client.post(
            "/v1/" + wire,
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json=payload,
        )
    assert result.status_code == 200
    assert ("ANSWER" if wire == "messages" else "response.completed") in result.text
    assert len(seen) == 2 and seen[0] == seen[1]
    assert len(leases) == 2 and all(lease.release.await_count == 1 for lease in leases)
    assert owner.waiting == 0


@pytest.mark.asyncio
async def test_recovery_checks_do_not_bypass_reported_provider_cooldown():
    settings = Settings(allow_paid_api_models=True, deepseek_api_key="synthetic")
    pool = freeze_pool(AutomaticFreePool(), [model()])
    pool.record_failure(
        settings,
        model(),
        ExecutionFailure(
            FailureKind.RATE_LIMIT, 429, "synthetic", False, retry_after_seconds=3600
        ),
    )
    original = pool.cooldown(settings, model()).copy()
    attempts = 0

    async def create():
        nonlocal attempts
        attempts += 1
        try:
            await pool.select(settings, {})
        except ExecutionFailure as error:
            assert error.recovery_safe
            return failure(error.status_code)
        pytest.fail("A cooling provider was selected")

    owner = CapacityRecovery(
        pool,
        lambda: settings,
        wait_seconds=0.025,
        check_seconds=0.001,
        heartbeat_seconds=0.001,
    )
    await collect(await owner.respond(create, stream=True, wire="messages"))
    assert attempts > 1
    assert pool.cooldown(settings, model()) == original
    assert pool._last_attempt is not None and pool._last_attempt["state"] == "failed"


def test_three_provider_failures_survive_wait_and_rotate_to_new_alternative(
    monkeypatch,
):
    from free_claude_code.api import free_chat_routes

    settings = Settings(
        allow_paid_api_models=True,
        deepseek_api_key="synthetic",
        open_router_api_key="synthetic",
    )
    app = create_app(
        SimpleNamespace(
            requests=SimpleNamespace(current_settings=lambda: settings),
            admin=SimpleNamespace(admin_status=None),
            prepare_chat_body=prepare_chat_body,
        )
    )
    models = [
        FreeModel("deepseek", f"failing-{i}", 1048576, 8192, True, False, "paid_api")
        for i in range(20)
    ]
    other = FreeModel("open_router", "working", 1048576, 8192, True, False, "paid_api")
    pool = freeze_pool(app.state.free_pool, models)

    async def refresh(settings, *, force=False):
        if force:
            pool._cooldowns.clear()
            pool._catalog = (*models, other)

    pool.refresh = refresh
    owner = app.state.capacity_recovery
    owner.wait_seconds, owner.check_seconds, owner.heartbeat_seconds = 1, 0.002, 0.001
    calls = []

    def serve(request):
        calls.append(request.url.host)
        if request.url.host == "api.deepseek.com":
            return httpx.Response(503, json={"error": "synthetic unavailable"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ALTERNATIVE_OK"}}]}
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        free_chat_routes.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    with TestClient(app) as client:
        result = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + settings.proxy_auth_token},
            json={"messages": [{"role": "user", "content": "synthetic"}]},
        )
    assert result.status_code == 200 and "ALTERNATIVE_OK" in result.text
    assert calls == ["api.deepseek.com"] * 3 + ["openrouter.ai"]
    assert current_route_budget() is None


@pytest.mark.asyncio
async def test_executor_limits_siblings_and_captures_budget_beyond_context_scope():
    settings = Settings(allow_paid_api_models=True)
    models = [
        FreeModel("deepseek", f"failing-{i}", 1048576, 8192, True, False, "paid_api")
        for i in range(10)
    ]
    pool = freeze_pool(AutomaticFreePool(), models)
    calls = []

    async def stream(request, **kwargs):
        calls.append(request.model)
        raise ExecutionFailure(FailureKind.UPSTREAM, 503, "synthetic", False)
        yield  # pragma: no cover

    async def resolve(_):
        return SimpleNamespace(stream_messages=stream)

    budget = RouteAttemptBudget()
    executor = ProviderExecutor(resolve, progress_timeout_seconds=1)
    with use_route_budget(budget):
        executor.configure_free_routing(pool, settings, models)
    request = MessagesRequest(
        model="auto",
        max_tokens=1024,
        messages=[{"role": "user", "content": "synthetic"}],
    )
    routed = ModelRouter(
        settings, free_targets=tuple(m.ref for m in models)
    ).resolve_messages_request(request)
    with pytest.raises(ExecutionFailure):
        async for _ in executor.stream_messages(
            routed, raw_log_payload={}, request_id="synthetic"
        ):
            pass
    assert len(calls) == 3 and budget.failures == {"deepseek": 3}
    pool._cooldowns.clear()
    with use_route_budget(budget), pytest.raises(ExecutionFailure):
        await pool.select(settings, {})
    with use_route_budget(RouteAttemptBudget()):
        assert len(await pool.select(settings, {})) == 10


@pytest.mark.asyncio
async def test_available_next_round_runs_immediately_without_wait():
    owner = recovery(check_seconds=60)
    owner.pool.select = AsyncMock(return_value=(model(),))
    calls = 0

    async def create():
        nonlocal calls
        calls += 1
        if calls == 1:
            current_route_budget().record_failure("deepseek")
            return failure(503)
        return JSONResponse({"answer": "NEXT_PROVIDER"})

    response = await owner.respond(create, stream=False, wire="chat")
    assert response.status_code == 200 and calls == 2
    assert owner.waiting == 0 and owner.pool.refresh.await_count == 0
