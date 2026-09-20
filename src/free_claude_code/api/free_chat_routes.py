"""OpenAI Chat ingress sharing the automatic free-provider pool."""

import asyncio
import json
from dataclasses import replace

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse

from free_claude_code.application.free_pool import local_base
from free_claude_code.config.free_mode import free_request_body
from free_claude_code.config.free_providers import POLICY_BY_ID, provider_key
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.free_quota import MAX_QUOTA_BODY_BYTES, daily_free_quota
from free_claude_code.core.free_stream import FreeStreamCheck, retry_seconds

from .dependencies import get_settings, require_proxy_auth

router = APIRouter()
_ALLOWED = frozenset(
    {
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "stop",
        "response_format",
        "seed",
        "frequency_penalty",
        "presence_penalty",
    }
)


def chat_target(settings, model, payload):
    body = {k: v for k, v in payload.items() if k in _ALLOWED}
    body["model"] = model.model_id
    maximum = body.pop("max_completion_tokens", body.get("max_tokens", 8192))
    body["max_tokens"] = min(
        maximum if isinstance(maximum, int) and maximum > 0 else 8192,
        model.output_limit or 8192,
        8192,
    )
    if model.provider_id == "open_router" and model.billing == "free":
        body = free_request_body(body, model=model.model_id)
    if model.provider_id == "gemini":
        body["model"] = model.model_id.removeprefix("models/")
    policy = POLICY_BY_ID[model.provider_id]
    base = (
        local_base(settings, model.provider_id)
        if policy.mode == "local"
        else PROVIDER_CATALOG[model.provider_id].default_base_url.rstrip("/")
    )
    key = provider_key(settings, model.provider_id)
    return (
        base + "/chat/completions",
        ({"Authorization": "Bearer " + key} if key else {}),
        body,
    )


def failure_response(failure, quota=None):
    return JSONResponse(
        {
            "error": {
                "message": failure.message,
                "type": "rate_limit_error"
                if failure.status_code == 429
                else "free_pool_unavailable",
                "code": "free_daily_quota_exhausted"
                if quota
                else "free_pool_unavailable",
            }
        },
        failure.status_code,
        headers=quota.response_headers()
        if quota
        else {"x-should-retry": "false", "Cache-Control": "no-store"},
    )


@router.post("/v1/chat/completions")
async def free_chat(
    request: Request,
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_proxy_auth),
):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4 * 1024 * 1024:
            raise HTTPException(
                413, "Request exceeds 4 MB; compact the conversation first"
            )
    try:
        payload = json.loads(raw)
    except ValueError, UnicodeError:
        raise HTTPException(400, "Invalid JSON") from None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("messages"), list)
        or not payload["messages"]
    ):
        raise HTTPException(400, "A nonempty messages array is required")
    payload = {k: v for k, v in payload.items() if k in _ALLOWED}
    pool = request.app.state.free_pool
    try:
        models = await pool.select(settings, {**payload, "_fcc_wire_api": "chat"})
    except ExecutionFailure as failure:
        return failure_response(failure)
    stream = payload.get("stream") is True
    client = httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=httpx.Timeout(45, connect=10)
    )
    response = None
    last = ExecutionFailure(
        FailureKind.UNAVAILABLE,
        503,
        "Every eligible fallback is cooling down. See Admin > Routing controls.",
        False,
    )
    quota = None
    request_id = getattr(request.state, "request_id", None)
    handed_off = False
    try:
        for model in models:
            if pool.cooldown(settings, model):
                continue
            url, headers, body = chat_target(settings, model, payload)
            body["stream"] = stream
            quota = None
            try:
                pool.record_attempt(model, request_id=request_id)
                response = await client.send(
                    client.build_request("POST", url, headers=headers, json=body),
                    stream=True,
                )
                quota = None
                if response.status_code != 200:
                    raw_error = bytearray()
                    async for part in response.aiter_bytes():
                        raw_error.extend(
                            part[: MAX_QUOTA_BODY_BYTES + 1 - len(raw_error)]
                        )
                        if len(raw_error) > MAX_QUOTA_BODY_BYTES:
                            break
                    if model.provider_id == "open_router":
                        quota = daily_free_quota(
                            bytes(raw_error),
                            status_code=response.status_code,
                            headers=response.headers,
                        )
                    status = (
                        response.status_code
                        if response.status_code in {400, 401, 402, 403, 404, 413, 429}
                        else 502
                    )
                    last = (
                        quota.failure()
                        if quota
                        else ExecutionFailure(
                            FailureKind.RATE_LIMIT
                            if status == 429
                            else FailureKind.UNAVAILABLE,
                            status,
                            f"Provider {model.provider_id} rejected this request (HTTP {status}). Only enabled routing categories may be tried.",
                            False,
                        )
                    )
                    last = replace(
                        last, retry_after_seconds=retry_seconds(response.headers)
                    )
                    pool.record_failure(settings, model, last, request_id=request_id)
                    await response.aclose()
                    continue
                if not stream:
                    data = bytearray()
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data) > 8 * 1024 * 1024:
                            raise ValueError("Chat result exceeds response limit")
                    await response.aclose()
                    result = json.loads(data)
                    if not isinstance(result, dict) or not result.get("choices"):
                        raise ValueError("Invalid chat result")
                    pool.record_success(model, request_id=request_id)
                    return JSONResponse(
                        result,
                        headers={
                            "X-FCC-Provider": model.provider_id,
                            "X-FCC-Billing": model.billing,
                            **(
                                {"X-FCC-Free-Provider": model.provider_id}
                                if model.billing == "free"
                                else {}
                            ),
                            "Cache-Control": "no-store",
                        },
                    )
                iterator = response.aiter_bytes()
                check = FreeStreamCheck(model.provider_id)
                first = bytearray()
                async with asyncio.timeout(45):
                    async for part in iterator:
                        check.feed(part)
                        first.extend(part)
                        if len(first) > 2 * 1024 * 1024:
                            raise ValueError("No bounded initial SSE event")
                        if b"data:" in first and b"\n\n" in first.replace(
                            b"\r\n", b"\n"
                        ):
                            break
                    else:
                        raise ValueError("Empty provider stream")

                async def events(
                    upstream=response,
                    selected=model,
                    remaining=iterator,
                    initial=bytes(first),
                    checker=check,
                ):
                    try:
                        yield initial
                        async for part in remaining:
                            checker.feed(part)
                            yield part
                        if not checker.complete:
                            raise ExecutionFailure(
                                FailureKind.UPSTREAM,
                                502,
                                "Free provider stream ended without completion.",
                                False,
                            )
                        pool.record_success(selected, request_id=request_id)
                    except (httpx.HTTPError, ExecutionFailure) as error:
                        failure = (
                            error
                            if isinstance(error, ExecutionFailure)
                            else ExecutionFailure(
                                FailureKind.UNAVAILABLE,
                                502,
                                "Free provider stream interrupted",
                                False,
                            )
                        )
                        pool.record_failure(
                            settings, selected, failure, request_id=request_id
                        )
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "error": {
                                        "message": failure.message,
                                        "type": "upstream_error",
                                    }
                                }
                            )
                            + "\n\n"
                        )
                    finally:
                        await upstream.aclose()
                        await client.aclose()

                handed_off = True
                return StreamingResponse(
                    events(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-store",
                        "X-FCC-Provider": model.provider_id,
                        "X-FCC-Billing": model.billing,
                        **(
                            {"X-FCC-Free-Provider": model.provider_id}
                            if model.billing == "free"
                            else {}
                        ),
                    },
                )
            except (
                httpx.HTTPError,
                ValueError,
                TimeoutError,
                ExecutionFailure,
            ) as error:
                if response is not None:
                    await response.aclose()
                quota = None
                last = (
                    error
                    if isinstance(error, ExecutionFailure)
                    else ExecutionFailure(
                        FailureKind.UNAVAILABLE,
                        502,
                        f"Provider {model.provider_id} failed before output. Only enabled routing categories may be tried.",
                        False,
                    )
                )
                pool.record_failure(settings, model, last, request_id=request_id)
        return failure_response(last, quota)
    except asyncio.CancelledError:
        if response is not None:
            await response.aclose()
        raise
    finally:
        if not handed_off:
            await client.aclose()
