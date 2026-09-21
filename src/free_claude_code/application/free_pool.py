"""Free-default discovery, opt-in billing routes, and shared failure cooldowns."""

import asyncio
import hashlib
import ipaddress
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import zip_longest
from time import time
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from free_claude_code.application.route_health import VERIFIED_TTL_SECONDS, RouteHealth
from free_claude_code.config.free_mode import MIN_CONTEXT_TOKENS
from free_claude_code.config.free_model_preferences import (
    FREE_MODEL_FAMILIES,
    free_model_family,
    free_preference_rank,
    preferred_free_families,
)
from free_claude_code.config.free_providers import (
    POLICY_BY_ID,
    ROUTING_PROVIDERS,
    explicitly_zero_priced,
    provider_key,
    zen_free_chat_ids,
)
from free_claude_code.config.paths import config_dir_path
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.provider_model_defaults import DOCUMENTED_MODEL_DEFAULTS
from free_claude_code.core.failures import BillingLimit, ExecutionFailure, FailureKind
from free_claude_code.core.fallback_order import reserve_provider_candidates
from free_claude_code.core.free_accounts import (
    FreeAccountConfirmations,
    credential_fingerprint,
)
from free_claude_code.core.google_errors import GoogleAccessError, google_access_message
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
)

CATALOG_TTL = 300
MAX_CATALOG_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FreeModel:
    provider_id: str
    model_id: str
    context: int | None
    output_limit: int | None
    tools: bool
    vision: bool
    price_basis: str

    @property
    def ref(self):
        return f"{self.provider_id}/{self.model_id}"

    @property
    def billing(self):
        return (
            self.price_basis
            if self.price_basis in {"subscription", "paid_api"}
            else "free"
        )


def positive_int(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def local_base(settings, provider_id):
    descriptor = PROVIDER_CATALOG[provider_id]
    base = getattr(settings, descriptor.base_url_attr).rstrip("/")
    parsed = urlsplit(base)
    host = parsed.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if (
        not loopback
        or parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Automatic local fallback requires a loopback URL")
    return base if base.endswith("/v1") else base + "/v1"


def request_needs(payload):
    # Conservative byte estimate; provider tokenizers may still reject a request.
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    maximum = payload.get("max_tokens", payload.get("max_output_tokens", 8192))
    output = min(positive_int(maximum) or 8192, 8192)
    context = len(raw.encode("utf-8")) // 2 + output
    vision = bool(re.search(r'"(?:type)"\s*:\s*"(?:image|image_url|input_image)"', raw))
    return context, bool(payload.get("tools")), vision


class AutomaticFreePool:
    def __init__(self, *, subscriptions=None):
        self._subscriptions = subscriptions
        self._connected = {}
        self._lock = asyncio.Lock()
        self._catalog = ()
        self._reports = []
        self._refreshed = 0.0
        self._fingerprint = None
        self._settings = None
        self._cooldowns = {}
        self._last_success = None
        self._last_success_details = None
        self._last_attempt = None
        self._load_cooldowns()
        self._health = RouteHealth()

    def _load_cooldowns(self):
        try:
            entries = json.loads(
                read_private_text(config_dir_path() / "free-cooldowns.json")
            )
            if isinstance(entries, dict):
                self._cooldowns = {
                    k: v
                    for k, v in entries.items()
                    if isinstance(v, dict)
                    and isinstance(v.get("until"), int | float)
                    and time() < v["until"] <= time() + 172800
                }
        except OSError, ValueError:
            pass

    def _save_cooldowns(self):
        self._cooldowns = {
            k: v for k, v in self._cooldowns.items() if v["until"] > time()
        }
        with suppress(OSError):
            atomic_write_private_text(
                config_dir_path() / "free-cooldowns.json", json.dumps(self._cooldowns)
            )

    def _scope(self, settings, provider_id):
        policy = POLICY_BY_ID[provider_id]
        identity = (
            self._connected.get(provider_id, "disconnected")
            if PROVIDER_CATALOG[provider_id].auth_kind == "connected_account"
            else str(getattr(settings, PROVIDER_CATALOG[provider_id].base_url_attr, ""))
            if policy.mode == "local"
            else provider_key(settings, provider_id)
        )
        return provider_id + ":" + credential_fingerprint(identity)

    def _health_key(self, settings, model):
        return (
            self._scope(settings, model.provider_id)
            + ":"
            + model.billing
            + ":"
            + model.model_id
        )

    def model_health(self, settings, model):
        return self._health.status(
            self._health_key(settings, model), cooldown=self.cooldown(settings, model)
        )

    def health_rank(self, settings, model):
        return {
            "VERIFIED": 0,
            "UNTESTED": 1,
            "STALE": 1,
            "RECHECK_DUE": 2,
            "FAILED": 3,
        }[self.model_health(settings, model)["state"]]

    def _config_fingerprint(self, settings):
        approvals = FreeAccountConfirmations()
        values = [
            (
                p.provider_id,
                self._scope(settings, p.provider_id),
                approvals.confirmed(settings, p.provider_id)
                if p.mode == "free_account"
                else True,
            )
            for p in ROUTING_PROVIDERS
        ]
        values.append(
            (settings.allow_subscription_models, settings.allow_paid_api_models)
        )
        return hashlib.sha256(repr(values).encode()).hexdigest()

    async def _fetch(self, client, url, *, key="", body=None, native_gemini=False):
        headers = (
            {
                ("x-goog-api-key" if native_gemini else "Authorization"): (
                    key if native_gemini else "Bearer " + key
                )
            }
            if key
            else {}
        )
        async with asyncio.timeout(12):
            async with client.stream(
                "POST" if body is not None else "GET", url, headers=headers, json=body
            ) as response:
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_CATALOG_BYTES:
                        raise ValueError("Catalog exceeds bounded discovery size")
                if native_gemini and response.is_error:
                    try:
                        message = google_access_message(json.loads(raw))
                    except ValueError:
                        message = None
                    if message:
                        raise GoogleAccessError(message)
                response.raise_for_status()
                return bytes(raw)

    async def refresh(self, settings, *, force=False):
        self._connected = (
            await self._subscriptions.identities()
            if self._subscriptions is not None
            else {}
        )
        fingerprint = self._config_fingerprint(settings)
        async with self._lock:
            if (
                not force
                and fingerprint == self._fingerprint
                and time() - self._refreshed < CATALOG_TTL
            ):
                return
            self._settings = settings
            approvals = FreeAccountConfirmations()
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=10
            ) as client:
                try:
                    metadata = json.loads(
                        await self._fetch(client, "https://models.dev/api.json")
                    )
                except httpx.HTTPError, ValueError, TimeoutError:
                    metadata = {}
                semaphore = asyncio.Semaphore(4)

                async def discover(policy):
                    key = provider_key(settings, policy.provider_id)
                    report = {
                        "provider": policy.provider_id,
                        "name": PROVIDER_CATALOG[policy.provider_id].display_name,
                        "mode": policy.mode,
                        "source": policy.source,
                        "models": 0,
                        "state": "MISSING_KEY",
                        "account_confirmed": approvals.confirmed(
                            settings, policy.provider_id
                        )
                        if policy.mode == "free_account"
                        else False,
                    }
                    if (
                        PROVIDER_CATALOG[policy.provider_id].auth_kind
                        == "connected_account"
                    ):
                        enabled = (
                            settings.allow_subscription_models
                            if policy.mode == "subscription"
                            else settings.allow_paid_api_models
                        )
                        report["state"] = (
                            "DISABLED" if not enabled else "CONNECT_ACCOUNT"
                        )
                        if not enabled:
                            return [], report
                        if (
                            policy.provider_id not in self._connected
                            or self._subscriptions is None
                        ):
                            return [], report
                        try:
                            async with semaphore, asyncio.timeout(25):
                                infos = await self._subscriptions.discover(
                                    policy.provider_id
                                )
                            report["catalog_models"] = len(infos)
                            report["below_context_minimum"] = sum(
                                i.context_window_tokens is not None
                                and i.context_window_tokens < MIN_CONTEXT_TOKENS
                                for i in infos
                            )
                            models = []
                            for info in infos:
                                secondary = (
                                    metadata.get(policy.metadata_id, {})
                                    .get("models", {})
                                    .get(info.model_id, {})
                                )
                                tools = (
                                    info.supports_tools
                                    if info.supports_tools is not None
                                    else secondary.get("tool_call") is True
                                )
                                context = info.context_window_tokens
                                if (
                                    tools
                                    and context is not None
                                    and context >= MIN_CONTEXT_TOKENS
                                ):
                                    models.append(
                                        FreeModel(
                                            policy.provider_id,
                                            info.model_id,
                                            context,
                                            info.max_output_tokens,
                                            True,
                                            bool(
                                                info.input_modalities
                                                and "image" in info.input_modalities
                                            ),
                                            policy.mode,
                                        )
                                    )
                            report.update(
                                models=len(models),
                                state="ELIGIBLE" if models else "NO_ELIGIBLE_MODELS",
                                coverage="COMPLETE",
                            )
                            return models, report
                        except Exception:
                            report["state"] = "DISCOVERY_UNAVAILABLE"
                            return [], report
                    if policy.mode == "paid_api" and not settings.allow_paid_api_models:
                        report["state"] = "DISABLED"
                        return [], report
                    if policy.mode != "local" and not key:
                        return [], report
                    if (
                        policy.mode == "free_account"
                        and not approvals.confirmed(settings, policy.provider_id)
                        and not settings.allow_paid_api_models
                    ):
                        report["state"] = "CONFIRM_FREE_ACCOUNT"
                        return [], report
                    async with semaphore:
                        try:
                            async with asyncio.timeout(25):
                                models, complete = await self._discover(
                                    client, settings, policy, metadata
                                )
                            report["catalog_models"] = len(models)
                            report["below_context_minimum"] = sum(
                                m.context is not None and m.context < MIN_CONTEXT_TOKENS
                                for m in models
                            )
                            models = [
                                m
                                for m in models
                                if m.context is not None
                                and m.context >= MIN_CONTEXT_TOKENS
                            ]
                            report.update(
                                models=len(models),
                                state="ELIGIBLE" if models else "NO_ELIGIBLE_MODELS",
                                coverage="COMPLETE" if complete else "PARTIAL",
                            )
                            return models, report
                        except GoogleAccessError as error:
                            report.update(
                                state="DISCOVERY_REJECTED", message=error.message
                            )
                        except httpx.HTTPStatusError as error:
                            report.update(
                                state="DISCOVERY_REJECTED",
                                http_status=error.response.status_code,
                            )
                        except (
                            httpx.HTTPError,
                            ValueError,
                            KeyError,
                            TypeError,
                            TimeoutError,
                        ):
                            report["state"] = "DISCOVERY_UNAVAILABLE"
                        return [], report

                results = await asyncio.gather(
                    *(discover(p) for p in ROUTING_PROVIDERS)
                )
            self._catalog = tuple(model for models, _ in results for model in models)
            self._reports = [report for _, report in results]
            self._refreshed = time()
            self._fingerprint = fingerprint

    async def _discover(self, client, settings, policy, metadata):
        provider = policy.provider_id
        if policy.mode == "local":
            return await self._discover_local(client, settings, provider)
        key = provider_key(settings, provider)
        base = PROVIDER_CATALOG[provider].default_base_url.rstrip("/")
        if provider == "open_router" and settings.allow_paid_api_models:
            try:
                credits = json.loads(
                    await self._fetch(client, base + "/credits", key=key)
                ).get("data", {})
                total, used = credits.get("total_credits"), credits.get("total_usage")
                paid_scope = self._scope(settings, provider) + ":paid_api"
                if isinstance(total, int | float) and isinstance(used, int | float):
                    if total <= used:
                        self._cooldowns[paid_scope] = {
                            "until": time() + 300,
                            "reason": "balance_exhausted",
                            "provider": provider,
                        }
                        self._save_cooldowns()
                    elif (
                        self._cooldowns.get(paid_scope, {}).get("reason")
                        == "balance_exhausted"
                    ):
                        self._cooldowns.pop(paid_scope, None)
                        self._save_cooldowns()
            except httpx.HTTPError, ValueError, TypeError, AttributeError, TimeoutError:
                pass
        if provider == "open_router":
            # A read-only key check can expose account-wide exhaustion before a
            # model-specific access error masks it. Keep the account body in RAM.
            try:
                account = json.loads(await self._fetch(client, base + "/key", key=key))
                daily = account.get("data", {}).get("free_model_daily_requests", {})
                remaining = daily.get("remaining")
                scope = self._scope(settings, provider)
                if (
                    isinstance(remaining, int)
                    and not isinstance(remaining, bool)
                    and remaining == 0
                ):
                    existing = self._cooldowns.get(scope, {})
                    until = (
                        max(time() + 300, existing.get("until", 0))
                        if existing.get("reason") == "daily_quota_exhausted"
                        else time() + 300
                    )
                    self._cooldowns[scope] = {
                        "until": until,
                        "reason": "daily_quota_exhausted",
                        "provider": provider,
                    }
                    self._save_cooldowns()
                elif (
                    isinstance(remaining, int)
                    and not isinstance(remaining, bool)
                    and remaining > 0
                    and self._cooldowns.get(scope, {}).get("reason")
                    == "daily_quota_exhausted"
                ):
                    self._cooldowns.pop(scope, None)
                    self._save_cooldowns()
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                TimeoutError,
                AttributeError,
            ):
                # Unavailable quota metadata is unknown, not zero.
                pass
        if provider == "gemini":
            # Use the native catalog for limits/capabilities; never put keys in URLs.
            base = "https://generativelanguage.googleapis.com/v1beta"
        catalog_url = base + (
            "/chat/completions/models" if provider == "inception" else "/models"
        )
        next_url = catalog_url
        rows = []
        complete = False
        visited = set()
        for _ in range(8):
            if next_url in visited:
                break
            visited.add(next_url)
            payload = json.loads(
                await self._fetch(
                    client, next_url, key=key, native_gemini=provider == "gemini"
                )
            )
            page = payload.get("models" if provider == "gemini" else "data")
            if not isinstance(page, list):
                raise ValueError("Invalid model catalog")
            rows.extend(page)
            token = payload.get("nextPageToken")
            links = payload.get("links") or {}
            link = links.get("next") if isinstance(links, dict) else None
            if token and provider == "gemini":
                next_url = catalog_url + "?" + urlencode({"pageToken": token})
            elif isinstance(link, str) and link:
                next_url = urljoin(catalog_url, link)
                parsed = urlsplit(next_url)
                original = urlsplit(catalog_url)
                if (parsed.scheme, parsed.netloc, parsed.path) != (
                    original.scheme,
                    original.netloc,
                    original.path,
                ):
                    raise ValueError("Catalog pagination left the provider endpoint")
            else:
                complete = not payload.get("has_more")
                break
        if isinstance(payload.get("total_count"), int) and payload["total_count"] > len(
            rows
        ):
            complete = False
        reference = (
            metadata.get(policy.metadata_id, {}).get("models", {})
            if isinstance(metadata, dict)
            else {}
        )
        zen_free = set()
        if policy.mode == "zen_free":
            zen_free = zen_free_chat_ids(
                (await self._fetch(client, "https://opencode.ai/docs/zen/")).decode(
                    "utf-8"
                )
            )
        models = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("name" if provider == "gemini" else "id")
            if (
                not isinstance(model_id, str)
                or not model_id
                or len(model_id) > 250
                or row.get("active") is False
            ):
                continue
            # An opaque router can select a smaller backing model even when its
            # aggregate catalog entry advertises a large maximum context.
            if model_id in {
                "openrouter/free",
                "kilo-auto/free",
                "openrouter/auto",
                "auto",
            }:
                continue
            basis = policy.mode
            if (
                provider == "open_router"
                and settings.allow_paid_api_models
                and not model_id.endswith(":free")
            ):
                basis = "paid_api"
            if (
                (policy.mode == "zero_price" and not explicitly_zero_priced(row))
                or (policy.mode == "zen_free" and model_id not in zen_free)
                or (
                    policy.mode == "free_account"
                    and not FreeAccountConfirmations().confirmed(settings, provider)
                )
            ):
                basis = "paid_api"
            if basis == "paid_api" and not settings.allow_paid_api_models:
                continue
            # Command Code separates Anthropic and Chat models. This adapter
            # currently supports its documented Chat endpoint only.
            if provider == "commandcode" and "/chat/completions" not in row.get(
                "supported_endpoints", []
            ):
                continue
            if provider == "gemini" and "generateContent" not in row.get(
                "supportedGenerationMethods", []
            ):
                continue
            info = DOCUMENTED_MODEL_DEFAULTS.get(provider, {}).get(
                model_id
            ) or reference.get(
                model_id, reference.get(model_id.removeprefix("models/"), {})
            )
            if not info and provider in {"commandcode", "cline_pass"}:
                # Exact upstream names only. Primary gateway context still wins;
                # registry metadata never establishes account pricing.
                info = (
                    metadata.get("openrouter", {}).get("models", {}).get(model_id, {})
                )
            params = row.get("supported_parameters", [])
            caps = row.get("capabilities", {})
            context = (
                positive_int(row.get("context_length"))
                or positive_int(row.get("context_window"))
                or positive_int(row.get("max_context_length"))
                or positive_int(row.get("inputTokenLimit"))
                or positive_int(info.get("limit", {}).get("context"))
            )
            provider_context = positive_int(
                row.get("top_provider", {}).get("context_length")
            )
            if provider_context and context:
                context = min(context, provider_context)
            output = (
                positive_int(row.get("top_provider", {}).get("max_completion_tokens"))
                or positive_int(row.get("max_output_tokens"))
                or positive_int(row.get("max_output_length"))
                or positive_int(row.get("outputTokenLimit"))
                or positive_int(info.get("limit", {}).get("output"))
            )
            tools = (
                "tools" in params
                or "tools" in row.get("supported_features", [])
                or caps.get("tool_calling") is True
                or caps.get("tools") is True
                or caps.get("function_calling") is True
                or info.get("tool_call") is True
            )
            if (
                caps.get("tool_calling") is False
                or caps.get("tools") is False
                or caps.get("function_calling") is False
            ):
                tools = False
            modalities = row.get("architecture", {}).get(
                "input_modalities",
                row.get(
                    "input_modalities", info.get("modalities", {}).get("input", [])
                ),
            )
            # Unknown capabilities are visible as exclusions, never claimed ready.
            if not tools or context is None:
                continue
            models.append(
                FreeModel(
                    provider,
                    model_id,
                    context,
                    output,
                    tools,
                    "image" in modalities,
                    basis,
                )
            )
        return list({m.ref: m for m in models}.values()), complete

    async def _discover_local(self, client, settings, provider):
        base = local_base(settings, provider)
        if provider != "ollama":
            # OpenAI model lists alone do not prove tool support or loaded context.
            payload = json.loads(await self._fetch(client, base + "/models"))
            result = []
            for row in payload.get("data", []):
                context = positive_int(row.get("context_length")) or positive_int(
                    row.get("max_context_length")
                )
                caps = row.get("capabilities", {})
                if context and isinstance(caps, dict) and caps.get("tools") is True:
                    result.append(
                        FreeModel(
                            provider,
                            row["id"],
                            context,
                            None,
                            True,
                            caps.get("vision") is True,
                            "local",
                        )
                    )
            return result, not payload.get("has_more")
        root = base.removesuffix("/v1")
        payload = json.loads(await self._fetch(client, root + "/api/tags"))
        result = []
        for row in payload.get("models", [])[:16]:
            name = row.get("name", "")
            if not name or "cloud" in name.lower() or row.get("remote_host"):
                continue
            info = json.loads(
                await self._fetch(client, root + "/api/show", body={"model": name})
            )
            if info.get("remote_host") or "tools" not in info.get("capabilities", []):
                continue
            # Ollama's loaded context is explicit; a model's theoretical maximum is insufficient.
            context = 4096
            match = re.search(
                r"(?m)^\s*num_ctx\s+(\d+)\s*$", info.get("parameters", "")
            )
            if match:
                context = int(match[1])
            limits = [
                positive_int(value)
                for key, value in info.get("model_info", {}).items()
                if key.endswith(".context_length")
            ]
            known_limits = [value for value in limits if value]
            if not known_limits:
                continue
            context = min(context, min(known_limits))
            result.append(
                FreeModel(
                    provider,
                    name,
                    context,
                    None,
                    True,
                    "vision" in info.get("capabilities", []),
                    "local",
                )
            )
        return result, len(payload.get("models", [])) <= 16

    def cooldown(self, settings, model):
        scope = self._scope(settings, model.provider_id)
        paid_scope = scope + ":" + model.billing
        for key in (
            scope,
            scope + ":" + model.model_id,
            paid_scope,
            paid_scope + ":" + model.model_id,
        ):
            entry = self._cooldowns.get(key)
            if entry and entry["until"] > time():
                if (
                    model.billing != "free"
                    and entry.get("reason") == "daily_quota_exhausted"
                ):
                    continue
                return entry
        return None

    @staticmethod
    def matches_selection(settings, model):
        return (
            model.provider_id == settings.routing_selected_provider
            and model.billing == settings.routing_selected_billing
            and (
                settings.routing_selected_model is None
                or model.model_id == settings.routing_selected_model
            )
        )

    def selection_status(self, settings):
        matches = [m for m in self._catalog if self.matches_selection(settings, m)]
        disabled = (settings.routing_disabled_providers or "").split(",")
        return {
            "mode": "selected" if settings.routing_selected_provider else "automatic",
            "provider": settings.routing_selected_provider,
            "model": settings.routing_selected_model,
            "billing": settings.routing_selected_billing,
            "fallback": True,
            "eligible_models": len(matches),
            "available_models": sum(
                not self.cooldown(settings, m)
                and m.provider_id not in disabled
                and (m.billing != "subscription" or settings.allow_subscription_models)
                and (m.billing != "paid_api" or settings.allow_paid_api_models)
                for m in matches
            ),
        }

    async def select(self, settings, payload):
        await self.refresh(settings)
        context, tools, vision = request_needs(payload)
        disabled = set((settings.routing_disabled_providers or "").split(","))
        eligible = [
            m
            for m in self._catalog
            if not self.cooldown(settings, m)
            and m.provider_id not in disabled
            and (m.billing != "subscription" or settings.allow_subscription_models)
            and (m.billing != "paid_api" or settings.allow_paid_api_models)
            and (payload.get("_fcc_wire_api") != "chat" or m.billing != "subscription")
            and (not tools or m.tools)
            and (not vision or m.vision)
            and m.context is not None
            and m.context >= max(context, MIN_CONTEXT_TOKENS)
        ]
        groups_by_billing = {kind: [] for kind in settings.routing_priority.split(",")}
        preferences = preferred_free_families(settings.free_model_priority)
        provider_order = (settings.routing_provider_priority or "").split(",")
        provider_order = [p for p in provider_order if p] + [
            p.provider_id
            for p in ROUTING_PROVIDERS
            if p.provider_id not in provider_order
        ]
        for billing, provider_id in (
            (billing, provider_id)
            for billing in settings.routing_priority.split(",")
            for provider_id in provider_order
        ):
            models = [
                m
                for m in eligible
                if m.provider_id == provider_id and m.billing == billing
            ]
            models.sort(
                key=lambda m: (
                    self.health_rank(settings, m),
                    free_preference_rank(m.model_id, preferences)
                    if billing == "free"
                    else 0,
                    m.ref != self._last_success,
                    not any(
                        word in m.model_id.lower()
                        for word in (
                            "coder",
                            "deepseek",
                            "kimi",
                            "gpt-oss",
                            "nex",
                            "glm",
                        )
                    ),
                    -(m.context or 0),
                    m.model_id,
                )
            )
            if models:
                groups_by_billing[billing].append(models)
        # Honor category priority; rotate providers within a category. Reserve
        # one slot per later category so a large catalog cannot starve fallback.
        categories = [
            sorted(
                [m for row in zip_longest(*groups) for m in row if m is not None],
                key=lambda m: (
                    self.health_rank(settings, m),
                    free_preference_rank(m.model_id, preferences)
                    if billing == "free"
                    else 0,
                ),
            )
            for billing, groups in groups_by_billing.items()
            if groups
        ]
        if settings.routing_selected_provider:
            selected = [
                m
                for category in categories
                for m in category
                if self.matches_selection(settings, m)
            ][:1]
            categories = ([selected] if selected else []) + [
                remaining
                for category in categories
                if (remaining := [m for m in category if m not in selected])
            ]
        ordered = []
        for index, category in enumerate(categories):
            budget = 12 - len(ordered) - (len(categories) - index - 1)
            ordered.extend(reserve_provider_candidates(category, budget))
        # Recent verified health precedes family preference. Stable sorting keeps
        # provider rotation within each health and preference tier.
        if not ordered:
            active = [
                entry
                for model in self._catalog
                if model.provider_id not in disabled
                and (entry := self.cooldown(settings, model))
            ]
            reset = min((v["until"] for v in active), default=None)
            detail = (
                f" Earliest retry check: {datetime.fromtimestamp(reset, UTC):%Y-%m-%d %H:%M:%S UTC}."
                if reset
                else ""
            )
            rate_limited_only = bool(active) and all(
                entry["reason"] in {"rate_limit", "daily_quota_exhausted"}
                for entry in active
            )
            raise ExecutionFailure(
                FailureKind.RATE_LIMIT
                if rate_limited_only
                else FailureKind.UNAVAILABLE,
                429 if rate_limited_only else 503,
                "No eligible "
                + (
                    "enabled"
                    if settings.allow_paid_api_models
                    or settings.allow_subscription_models
                    else "free"
                )
                + " provider with more than 512,000 context tokens is currently available for this request."
                + detail
                + self.availability_summary(settings)
                + " Open Admin > Routing controls for credentials, priority, capabilities and cooldowns."
                + (
                    " No paid fallback was enabled."
                    if not (
                        settings.allow_paid_api_models
                        or settings.allow_subscription_models
                    )
                    else " Only explicitly enabled billing categories were considered."
                ),
                False,
            )
        return tuple(ordered[:12])

    def availability_summary(self, settings):
        """Bounded, credential-free reasons from the current discovery snapshot."""
        details, missing = [], []
        disabled = set((settings.routing_disabled_providers or "").split(","))
        for report in self._reports:
            provider = report["provider"]
            if provider in disabled or report["state"] == "DISABLED":
                continue
            if report["state"] in {"MISSING_KEY", "CONNECT_ACCOUNT"}:
                missing.append(provider)
                continue
            reasons = set()
            for model in self._catalog:
                if model.provider_id == provider and (
                    entry := self.cooldown(settings, model)
                ):
                    reasons.add(
                        {
                            "daily_quota_exhausted": "free daily quota exhausted",
                            "balance_exhausted": "billing unavailable",
                            "request_budget_exceeded": "selected model exceeds request budget",
                            "key_spending_limit": "API key spending limit reached",
                            "in_flight_budget": "temporary spending budget occupied",
                            "temporary_model_failure": "temporary model outage",
                            "temporary_failure": "temporary provider outage",
                            "rate_limit": "rate limited",
                            "authentication_or_access": "authentication rejected",
                            "api_access_not_in_plan": "current plan does not include API access",
                            "model_or_request_incompatible": "model/request incompatible",
                        }.get(entry["reason"], "cooling down")
                    )
            if reasons:
                details.append(provider + ": " + ", ".join(sorted(reasons)))
            elif report.get("below_context_minimum"):
                details.append(provider + ": default context at or below 512,000")
            elif report["state"] == "CONFIRM_FREE_ACCOUNT":
                details.append(provider + ": free-account confirmation missing")
            elif report["state"].startswith("DISCOVERY_"):
                details.append(provider + ": catalog unavailable")
            elif report["state"] == "NO_ELIGIBLE_MODELS":
                details.append(provider + ": no eligible tool/context model")
        if missing:
            details.append(
                "Missing credentials: "
                + ", ".join(missing[:6])
                + (" and others" if len(missing) > 6 else "")
            )
        return (" Route status: " + "; ".join(details[:8]) + ".") if details else ""

    def record_attempt(self, model, *, request_id=None):
        self._last_attempt = {
            "provider": model.provider_id,
            "model": model.model_id,
            "billing": model.billing,
            "context_tokens": model.context,
            "request_id": request_id,
            "started_at": datetime.now(UTC).isoformat(),
            "state": "attempting",
        }

    def _finish_attempt(self, model, state, *, request_id=None, status_code=None):
        details = {
            "provider": model.provider_id,
            "model": model.model_id,
            "billing": model.billing,
            "context_tokens": model.context,
            "request_id": request_id,
            "finished_at": datetime.now(UTC).isoformat(),
            "state": state,
            "status_code": status_code,
        }
        if self._last_attempt is None or (
            self._last_attempt["request_id"] == request_id
            and self._last_attempt["provider"] == model.provider_id
            and self._last_attempt["model"] == model.model_id
        ):
            self._last_attempt = {**(self._last_attempt or {}), **details}
        return details

    def record_failure(
        self, settings, model, failure, *, request_id=None, affects_availability=True
    ):
        self._finish_attempt(
            model, "failed", request_id=request_id, status_code=failure.status_code
        )
        if not affects_availability:
            # Local request conversion says nothing about upstream availability.
            return
        self._health.record(
            self._health_key(settings, model),
            success=False,
            status_code=failure.status_code,
        )
        scope = self._scope(settings, model.provider_id)
        if model.billing != "free":
            scope += ":" + model.billing
        status = failure.status_code
        seconds = 60 if status == 429 else 45
        reason = "rate_limit" if status == 429 else "temporary_failure"
        if status == 403 and failure.provider_access_blocked:
            seconds, reason = 3600, "api_access_not_in_plan"
        elif (
            status == 402
            and failure.billing_limit is BillingLimit.REQUEST_BUDGET
            and model.billing == "paid_api"
        ):
            # Price-dependent rejection: keep cheaper siblings and free routes
            # eligible. A large request failing says nothing about their budget.
            scope += ":" + model.model_id
            seconds, reason = 60, "request_budget_exceeded"
        elif status == 402 and failure.billing_limit is BillingLimit.IN_FLIGHT_BUDGET:
            seconds = failure.retry_after_seconds or 45
            reason = "in_flight_budget"
        elif status == 402 and failure.billing_limit is BillingLimit.KEY_LIMIT:
            seconds, reason = 3600, "key_spending_limit"
        elif status in {401, 402}:
            seconds, reason = (
                3600,
                "balance_exhausted" if status == 402 else "authentication_or_access",
            )
        elif status in {400, 403, 404, 413}:
            scope += ":" + model.model_id
            seconds, reason = 300, "model_or_request_incompatible"
        elif status >= 500:
            # A single model/backend outage does not prove every sibling is down.
            scope += ":" + model.model_id
            reason = "temporary_model_failure"
        if "daily free-model request quota exhausted" in failure.message:
            seconds, reason = 300, "daily_quota_exhausted"
            match = re.search(
                r"Provider-reported reset: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC",
                failure.message,
            )
            if match:
                reset = (
                    datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=UTC)
                    .timestamp()
                )
                seconds = min(172800, max(1, reset - time()))
        if failure.retry_after_seconds is not None:
            seconds = max(seconds, min(172800, failure.retry_after_seconds))
        self._cooldowns[scope] = {
            "until": time() + seconds,
            "reason": reason,
            "provider": model.provider_id,
        }
        self._save_cooldowns()

    def record_success(self, model, *, request_id=None, settings=None, has_output=True):
        self._last_success = model.ref
        self._last_success_details = self._finish_attempt(
            model, "succeeded", request_id=request_id
        )
        settings = settings or self._settings
        if settings is not None and has_output:
            self._health.record(
                self._health_key(settings, model), success=True, status_code=200
            )

    async def status(self, settings, *, force=False):
        await self.refresh(settings, force=force)
        rows = []
        for report in self._reports:
            row = dict(report)
            models = [m for m in self._catalog if m.provider_id == row["provider"]]
            row["available_models"] = sum(
                not self.cooldown(settings, m)
                and m.provider_id
                not in (settings.routing_disabled_providers or "").split(",")
                for m in models
            )
            cooldowns = [
                entry for model in models if (entry := self.cooldown(settings, model))
            ]
            provider_cooldown = min(
                cooldowns, key=lambda entry: entry["until"], default=None
            )
            if (
                provider_cooldown
                and provider_cooldown["until"] > time()
                and row["available_models"] == 0
            ):
                row.update(
                    state="COOLDOWN",
                    retry_at=datetime.fromtimestamp(
                        provider_cooldown["until"], UTC
                    ).isoformat(),
                    reason=provider_cooldown["reason"],
                )
                row["cooldown_reasons"] = sorted(
                    {entry["reason"] for entry in cooldowns}
                )
            if row["provider"] in (settings.routing_disabled_providers or "").split(
                ","
            ):
                row["state"] = "EXCLUDED"
            row["model_ids"] = [m.model_id for m in models]
            row["model_details"] = [
                {
                    "id": m.model_id,
                    "context_tokens": m.context,
                    "max_output_tokens": m.output_limit,
                    "vision": m.vision,
                    "price_basis": m.price_basis,
                    "billing": m.billing,
                    "available": (
                        not self.cooldown(settings, m)
                        and m.provider_id
                        not in (settings.routing_disabled_providers or "").split(",")
                        and (
                            m.billing != "subscription"
                            or settings.allow_subscription_models
                        )
                        and (m.billing != "paid_api" or settings.allow_paid_api_models)
                    ),
                    "cooldown_reason": (self.cooldown(settings, m) or {}).get("reason"),
                    "health": self.model_health(settings, m),
                }
                for m in models
            ]
            row["verified_models"] = sum(
                m["available"] and m["health"]["state"] == "VERIFIED"
                for m in row["model_details"]
            )
            row["failed_models"] = sum(
                m["health"]["state"] == "FAILED" for m in row["model_details"]
            )
            row["health_state"] = (
                "VERIFIED"
                if row["verified_models"]
                else "FAILED"
                if row["state"]
                in {"COOLDOWN", "DISCOVERY_REJECTED", "DISCOVERY_UNAVAILABLE"}
                else "UNTESTED"
            )
            rows.append(row)
        return {
            "automatic": settings.auto_free_models,
            "selection": self.selection_status(settings),
            "free_model_priority": preferred_free_families(
                settings.free_model_priority
            ),
            "free_model_preferences": [
                {
                    "family": family,
                    "name": FREE_MODEL_FAMILIES[family],
                    "eligible_free_routes": [
                        m.ref
                        for m in self._catalog
                        if m.billing == "free"
                        and free_model_family(m.model_id) == family
                    ],
                    "available_free_routes": [
                        m.ref
                        for m in self._catalog
                        if m.billing == "free"
                        and free_model_family(m.model_id) == family
                        and not self.cooldown(settings, m)
                        and m.provider_id
                        not in (settings.routing_disabled_providers or "").split(",")
                    ],
                }
                for family in preferred_free_families(settings.free_model_priority)
            ],
            "allow_subscriptions": settings.allow_subscription_models,
            "allow_paid_api": settings.allow_paid_api_models,
            "billing_priority": settings.routing_priority.split(","),
            "provider_priority": [
                p for p in (settings.routing_provider_priority or "").split(",") if p
            ],
            "disabled_providers": [
                p for p in (settings.routing_disabled_providers or "").split(",") if p
            ],
            "billing_counts": {
                kind: sum(m.billing == kind for m in self._catalog)
                for kind in ("free", "subscription", "paid_api")
            },
            "minimum_context_tokens": MIN_CONTEXT_TOKENS,
            "reasoning_policy": settings.reasoning_policy.value,
            "health_ttl_seconds": VERIFIED_TTL_SECONDS,
            "verified_free_routes": [
                {
                    "provider": row["provider"],
                    "model": model["id"],
                    "checked_at": model["health"]["checked_at"],
                }
                for row in rows
                for model in row["model_details"]
                if model["billing"] == "free"
                and model["available"]
                and model["health"]["state"] == "VERIFIED"
            ],
            "refreshed_at": datetime.fromtimestamp(self._refreshed, UTC).isoformat(),
            "catalog_ttl_seconds": CATALOG_TTL,
            "eligible_models": len(self._catalog),
            "available_models": sum(r["available_models"] for r in rows),
            "last_success": self._last_success,
            "last_success_details": self._last_success_details,
            "latest_attempt": self._last_attempt,
            "providers": rows,
            "note": "Eligible means catalog and enabled billing-policy checks passed; it is not a successful inference guarantee. Free-account tiers rely on your acknowledgment. Paid APIs and subscriptions may consume allowance or purchased credits. This gateway has no monetary budget cap; configure spending controls with each provider. Every route requires more than 512,000 context tokens.",
        }
