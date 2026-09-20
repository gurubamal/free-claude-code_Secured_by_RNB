"""Authenticated free-routing status and local account acknowledgments."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from free_claude_code.config.free_mode import MIN_CONTEXT_TOKENS
from free_claude_code.core.free_accounts import FreeAccountConfirmations

from .admin_security import require_loopback_admin
from .dependencies import get_settings, require_proxy_auth

router = APIRouter()


class RouteSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["automatic", "selected"]
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=256)
    billing: Literal["free", "subscription", "paid_api"] = "free"

    @model_validator(mode="after")
    def validate_selection(self):
        if self.mode == "automatic" and (
            self.provider is not None or self.model is not None
        ):
            raise ValueError("Automatic mode does not take a provider or model")
        if self.mode == "selected" and not self.provider:
            raise ValueError("Choose a provider")
        if self.model is not None and (
            not self.model.strip() or not self.model.isprintable()
        ):
            raise ValueError("Choose an exact printable catalog model ID")
        return self


async def apply_route_selection(body: RouteSelectionPayload, request: Request):
    services = request.app.state.services
    settings = services.requests.current_settings()
    pool = request.app.state.free_pool
    if body.mode == "selected":
        if body.provider in (settings.routing_disabled_providers or "").split(","):
            raise HTTPException(
                400, "Provider is excluded. Enable it in Routing controls first."
            )
        if (body.billing == "paid_api" and not settings.allow_paid_api_models) or (
            body.billing == "subscription" and not settings.allow_subscription_models
        ):
            raise HTTPException(
                400, "This billing category is disabled. Enable it in Admin first."
            )
        await pool.refresh(settings, force=True)
        if not any(
            m.provider_id == body.provider
            and m.billing == body.billing
            and (body.model is None or m.model_id == body.model)
            and m.tools
            and m.context is not None
            and m.context >= MIN_CONTEXT_TOKENS
            for m in pool._catalog
        ):
            raise HTTPException(
                400,
                "No matching eligible model in the current catalog. Check provider setup, free eligibility and the 512k minimum. Nothing changed.",
            )
    result = await services.admin.apply_admin_config(
        {
            "AUTO_FREE_MODELS": True,
            "ROUTING_SELECTED_PROVIDER": body.provider,
            "ROUTING_SELECTED_MODEL": body.model,
            "ROUTING_SELECTED_BILLING": body.billing,
        }
    )
    if result.get("errors") or result.get("applied") is False:
        raise HTTPException(
            400, "Route selection could not be saved. Check Admin configuration."
        )
    active = services.requests.current_settings()
    if (
        not active.auto_free_models
        or active.routing_selected_provider != body.provider
        or active.routing_selected_model != body.model
        or active.routing_selected_billing != body.billing
    ):
        raise HTTPException(
            409,
            "Route selection is overridden by process settings or pending a restart. Check Admin before retrying.",
        )
    # Never expose the configuration response: it includes fields unrelated to routing.
    return await pool.status(active)


@router.post("/admin/api/free/selection")
async def admin_route_selection(body: RouteSelectionPayload, request: Request):
    return await apply_route_selection(body, request)


@router.post(
    "/v1/routing/selection",
    dependencies=[Depends(require_proxy_auth), Depends(require_loopback_admin)],
)
async def cli_route_selection(body: RouteSelectionPayload, request: Request):
    if (
        request.headers.get("origin") is not None
        or request.headers.get("sec-fetch-site") is not None
        or request.headers.get("x-fcc-route-control") != "1"
    ):
        raise HTTPException(
            403, "Use the local routing CLI or authenticated Admin controls."
        )
    return await apply_route_selection(body, request)


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
    free_model_priority: list[str] | None = None


@router.post("/admin/api/free/policy")
async def routing_policy(body: RoutingPolicyPayload, request: Request):
    services = request.app.state.services
    preferences = {}
    if body.free_model_priority is not None:
        preferences["FREE_MODEL_PRIORITY"] = (
            ",".join(body.free_model_priority) or "none"
        )
    result = await services.admin.apply_admin_config(
        {
            **preferences,
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
