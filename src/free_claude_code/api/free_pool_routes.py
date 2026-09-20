"""Authenticated free-routing status and local account acknowledgments."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from free_claude_code.core.free_accounts import FreeAccountConfirmations

from .dependencies import get_settings, require_proxy_auth

router = APIRouter()


class FreeAccountPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    no_paid_billing: StrictBool


class RoutingPolicyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_subscriptions: StrictBool
    allow_paid_api: StrictBool
    billing_priority: list[str]
    provider_priority: list[str]
    disabled_providers: list[str]


@router.post("/admin/api/free/policy")
async def routing_policy(body: RoutingPolicyPayload, request: Request):
    services = request.app.state.services
    result = await services.admin.apply_admin_config(
        {
            "ALLOW_SUBSCRIPTION_MODELS": body.allow_subscriptions,
            "ALLOW_PAID_API_MODELS": body.allow_paid_api,
            "ROUTING_PRIORITY": ",".join(body.billing_priority),
            "ROUTING_PROVIDER_PRIORITY": ",".join(body.provider_priority) or None,
            "ROUTING_DISABLED_PROVIDERS": ",".join(body.disabled_providers) or None,
        }
    )
    if result.get("errors"):
        raise HTTPException(
            400,
            "Routing settings were not applied; check the selected priority and provider IDs.",
        )
    return await request.app.state.free_pool.status(
        services.requests.current_settings(), force=True
    )


@router.get("/admin/free", response_class=HTMLResponse)
async def free_page():
    return (
        Path(__file__)
        .with_name("admin_static")
        .joinpath("free_pool.html")
        .read_text(encoding="utf-8")
    )


@router.get("/admin/api/free/status")
async def admin_status(request: Request, settings=Depends(get_settings)):
    return await request.app.state.free_pool.status(settings)


@router.post("/admin/api/free/refresh")
async def admin_refresh(request: Request, settings=Depends(get_settings)):
    return await request.app.state.free_pool.status(settings, force=True)


@router.post("/admin/api/free/accounts/{provider_id}")
async def confirm_account(
    provider_id: str,
    body: FreeAccountPayload,
    request: Request,
    settings=Depends(get_settings),
):
    try:
        FreeAccountConfirmations().set(settings, provider_id, body.no_paid_billing)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return await request.app.state.free_pool.status(settings, force=True)


@router.get("/v1/free/status")
async def client_status(
    request: Request, settings=Depends(get_settings), _auth=Depends(require_proxy_auth)
):
    return await request.app.state.free_pool.status(settings)
