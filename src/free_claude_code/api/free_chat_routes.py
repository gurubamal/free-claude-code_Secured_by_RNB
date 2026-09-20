"""Free-only OpenAI Chat adapter for clients that do not speak Messages/Responses."""

import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse

from free_claude_code.config.free_mode import free_request_body
from free_claude_code.config.settings import Settings
from free_claude_code.core.free_quota import MAX_QUOTA_BODY_BYTES, daily_free_quota

from .dependencies import get_settings, require_proxy_auth

router = APIRouter()
_ALLOWED = frozenset(
    {
        "model",
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


@router.post("/v1/chat/completions")
async def free_chat(
    request: Request,
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_proxy_auth),
):
    if not settings.open_router_api_key:
        raise HTTPException(503, "Configure your OpenRouter key in Admin")
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
    body = free_request_body({k: v for k, v in payload.items() if k in _ALLOWED})
    stream = body.get("stream") is True
    body["stream"] = stream
    client = httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=httpx.Timeout(180, connect=10)
    )
    response = None
    quota = None
    try:
        # Retry only before any bytes are delivered; never replay emitted tool calls.
        for attempt in range(3):
            response = await client.send(
                client.build_request(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": "Bearer " + settings.open_router_api_key},
                    json=body,
                ),
                stream=True,
            )
            if response.status_code == 429:
                error_body = bytearray()
                async for chunk in response.aiter_bytes():
                    error_body.extend(
                        chunk[: MAX_QUOTA_BODY_BYTES + 1 - len(error_body)]
                    )
                    if len(error_body) > MAX_QUOTA_BODY_BYTES:
                        break
                quota = daily_free_quota(
                    bytes(error_body), status_code=429, headers=response.headers
                )
                if quota is not None:
                    break
            if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                break
            await response.aclose()
            await asyncio.sleep(2**attempt)
        if response.status_code != 200:
            status = (
                response.status_code
                if response.status_code in {400, 401, 402, 403, 404, 413, 429}
                else 502
            )
            await response.aclose()
            await client.aclose()
            if quota is not None:
                return JSONResponse(
                    {
                        "error": {
                            "message": quota.message(),
                            "type": "rate_limit_error",
                            "code": "free_daily_quota_exhausted",
                        }
                    },
                    429,
                    headers=quota.response_headers(),
                )
            return JSONResponse(
                {
                    "error": {
                        "message": "Free model pool unavailable or request rejected. Paid models were not enabled. Retry later or compact the saved session.",
                        "type": "free_pool_unavailable",
                    }
                },
                status,
                headers={"Retry-After": "60"} if status == 429 else {},
            )
        if not stream:
            result = await response.aread()
            await response.aclose()
            await client.aclose()
            return JSONResponse(json.loads(result))
    except asyncio.CancelledError:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise
    except httpx.HTTPError, ValueError:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise HTTPException(
            502, "Free model connection failed; retry the saved session"
        ) from None

    async def events():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )
