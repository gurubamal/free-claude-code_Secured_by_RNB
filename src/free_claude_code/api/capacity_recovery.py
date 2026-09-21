"""Bounded, client-owned recovery after automatic routes fail before output."""

import asyncio
import json
from contextlib import suppress
from time import monotonic

from starlette.responses import JSONResponse, Response

from free_claude_code.application.route_budget import (
    PROVIDER_FAILURE_LIMIT,
    RouteAttemptBudget,
    use_route_budget,
)
from free_claude_code.core.failures import ExecutionFailure


def _safe(response):
    return getattr(response, "fcc_recovery_safe", False) and response.status_code in {
        402,
        429,
        502,
        503,
        504,
        529,
    }


async def _dispose(response):
    if close := getattr(response, "aclose", None):
        await close()


class CapacityRecovery:
    """One shared refresh throttle; no prompt storage or unattended inference."""

    def __init__(
        self,
        pool,
        settings,
        *,
        wait_seconds=900,
        check_seconds=30,
        refresh_seconds=60,
        heartbeat_seconds=15,
        max_waiting=8,
    ):
        self.pool = pool
        self.settings = settings
        self.wait_seconds = wait_seconds
        self.check_seconds = check_seconds
        self.refresh_seconds = refresh_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_waiting = max_waiting
        self.waiting = 0
        self._refreshed = float("-inf")
        self._refresh_lock = asyncio.Lock()

    def status(self):
        return {
            "enabled": self.settings().auto_free_models,
            "waiting_requests": self.waiting,
            "max_wait_seconds": self.wait_seconds,
            "check_seconds": self.check_seconds,
            "catalog_refresh_seconds": self.refresh_seconds,
            "max_waiting_requests": self.max_waiting,
            "requires_connected_client": True,
            "provider_failure_limit": PROVIDER_FAILURE_LIMIT,
        }

    async def refresh(self):
        async with self._refresh_lock:
            if monotonic() - self._refreshed >= self.refresh_seconds:
                self._refreshed = monotonic()
                await self.pool.refresh(self.settings(), force=True)

    async def respond(self, create, *, stream, wire):
        budget = RouteAttemptBudget()

        async def attempt():
            with use_route_budget(budget):
                attempted = sum(budget.failures.values())
                response = await create()
                # Exhaust eligible alternatives before waiting. Provider limits
                # bound these rounds even when a catalog has many models.
                while (
                    _safe(response)
                    and self.settings().auto_free_models
                    and sum(budget.failures.values()) > attempted
                ):
                    attempted = sum(budget.failures.values())
                    try:
                        await self.pool.select(self.settings(), {})
                    except ExecutionFailure:
                        break
                    response = await create()
                return response

        response = await attempt()
        if not _safe(response) or not self.settings().auto_free_models:
            return response
        return _RecoveryResponse(self, response, attempt, stream=stream, wire=wire)


class _RecoveryResponse(Response):
    def __init__(self, owner, first, create, *, stream, wire):
        super().__init__()
        self.owner, self.first, self.create = owner, first, create
        self.stream, self.wire = stream, wire

    async def __call__(self, scope, receive, send):
        owner = self.owner
        if owner.waiting >= owner.max_waiting:
            await self.first(scope, receive, send)
            return
        owner.waiting += 1
        response = self.first
        task = None
        deadline = monotonic() + owner.wait_seconds
        try:
            if self.stream:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"text/event-stream; charset=utf-8"),
                            (b"cache-control", b"no-store"),
                            (b"x-accel-buffering", b"no"),
                            (b"x-fcc-recovery", b"waiting"),
                        ],
                    }
                )
                await self._heartbeat(send)
            while _safe(response) and owner.settings().auto_free_models:
                if monotonic() >= deadline:
                    response = self._timeout()
                    break

                async def attempt(previous=response):
                    await asyncio.sleep(owner.check_seconds)
                    if not owner.settings().auto_free_models:
                        return previous
                    await owner.refresh()
                    # Each attempt acquires current settings and a fresh runtime lease.
                    return await self.create()

                task = asyncio.create_task(attempt(), name="fcc-capacity-recovery")
                while not task.done() and monotonic() < deadline:
                    remaining = deadline - monotonic()
                    await asyncio.wait(
                        {task}, timeout=min(owner.heartbeat_seconds, remaining)
                    )
                    if not task.done() and self.stream:
                        await self._heartbeat(send)
                if not task.done():
                    response = self._timeout()
                    break
                response = task.result()
                task = None

            if self.stream and response.status_code >= 400:
                body = json.loads(response.body)
                if self.wire == "responses":
                    error = body.get("error", {})
                    body = {
                        "type": "error",
                        "code": error.get("code")
                        or error.get("type")
                        or "server_error",
                        "message": error.get(
                            "message", "Provider capacity unavailable"
                        ),
                    }
                frame = "event: error\n" if self.wire != "chat" else ""
                frame += "data: " + json.dumps(body) + "\n\n"
                if self.wire == "chat":
                    frame += "data: [DONE]\n\n"
                await send({"type": "http.response.body", "body": frame.encode()})
            else:

                async def forward(message):
                    if not self.stream or message["type"] != "http.response.start":
                        await send(message)

                await response(scope, receive, forward)
        finally:
            try:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        result = await task
                        if result is not response:
                            await _dispose(result)
                await _dispose(response)
            finally:
                owner.waiting -= 1

    async def _heartbeat(self, send):
        # SSE comments are ignored by all three protocols; they are not model output.
        await send(
            {
                "type": "http.response.body",
                "body": b": FCC waiting for eligible provider capacity; automatic recovery active\n\n",
                "more_body": True,
            }
        )

    def _timeout(self):
        error = {
            "type": "overloaded_error",
            "message": "Automatic recovery reached its waiting limit without an eligible response. Check Admin > Routing controls and resume the saved session when capacity returns. No credits or spending limits were changed.",
        }
        return JSONResponse(
            {"type": "error", "error": error}
            if self.wire == "messages"
            else {"error": error},
            status_code=503,
            headers={"x-should-retry": "false"},
        )
