"""Fresh free-model discovery, capability selection, and shared provider cooldowns."""

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

from free_claude_code.config.free_providers import (
    FREE_PROVIDERS,
    POLICY_BY_ID,
    explicitly_zero_priced,
    provider_key,
    zen_free_chat_ids,
)
from free_claude_code.config.paths import config_dir_path
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.free_accounts import (
    FreeAccountConfirmations,
    credential_fingerprint,
)
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
)

CATALOG_TTL = 300
MIN_CONTEXT_TOKENS = 512000
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
    def __init__(self):
        self._lock = asyncio.Lock()
        self._catalog = ()
        self._reports = []
        self._refreshed = 0.0
        self._fingerprint = None
        self._settings = None
        self._cooldowns = {}
        self._last_success = None
        self._load_cooldowns()

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
            str(getattr(settings, PROVIDER_CATALOG[provider_id].base_url_attr, ""))
            if policy.mode == "local"
            else provider_key(settings, provider_id)
        )
        return provider_id + ":" + credential_fingerprint(identity)

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
            for p in FREE_PROVIDERS
        ]
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
                response.raise_for_status()
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_CATALOG_BYTES:
                        raise ValueError("Catalog exceeds bounded discovery size")
                return bytes(raw)

    async def refresh(self, settings, *, force=False):
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
                    if policy.mode != "local" and not key:
                        return [], report
                    if policy.mode == "free_account" and not approvals.confirmed(
                        settings, policy.provider_id
                    ):
                        report["state"] = "CONFIRM_FREE_ACCOUNT"
                        return [], report
                    async with semaphore:
                        try:
                            async with asyncio.timeout(25):
                                models, complete = await self._discover(
                                    client, settings, policy, metadata
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

                results = await asyncio.gather(*(discover(p) for p in FREE_PROVIDERS))
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
        catalog_url = base + "/models"
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
            if policy.mode == "zero_price" and not explicitly_zero_priced(row):
                continue
            if policy.mode == "zen_free" and model_id not in zen_free:
                continue
            if provider == "gemini" and "generateContent" not in row.get(
                "supportedGenerationMethods", []
            ):
                continue
            info = reference.get(
                model_id, reference.get(model_id.removeprefix("models/"), {})
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
                or positive_int(row.get("outputTokenLimit"))
                or positive_int(info.get("limit", {}).get("output"))
            )
            tools = (
                "tools" in params
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
                    policy.mode,
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
        for key in (scope, scope + ":" + model.model_id):
            entry = self._cooldowns.get(key)
            if entry and entry["until"] > time():
                return entry
        return None

    async def select(self, settings, payload):
        await self.refresh(settings)
        context, tools, vision = request_needs(payload)
        eligible = [
            m
            for m in self._catalog
            if not self.cooldown(settings, m)
            and (not tools or m.tools)
            and (not vision or m.vision)
            and m.context is not None
            and m.context >= max(context, MIN_CONTEXT_TOKENS)
        ]
        groups = []
        for policy in FREE_PROVIDERS:
            models = [m for m in eligible if m.provider_id == policy.provider_id]
            models.sort(
                key=lambda m: (
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
                groups.append(models)
        # Try independent providers before spending the request budget on siblings.
        ordered = [m for row in zip_longest(*groups) for m in row if m is not None]
        if self._last_success:
            ordered.sort(key=lambda m: m.ref != self._last_success)
        if not ordered:
            active = [
                entry
                for model in self._catalog
                if (entry := self.cooldown(settings, model))
            ]
            reset = min((v["until"] for v in active), default=None)
            detail = (
                f" Earliest retry check: {datetime.fromtimestamp(reset, UTC):%Y-%m-%d %H:%M:%S UTC}."
                if reset
                else ""
            )
            raise ExecutionFailure(
                FailureKind.RATE_LIMIT if active else FailureKind.UNAVAILABLE,
                429 if active else 503,
                "No eligible free provider with at least 512,000 context tokens is currently available for this request."
                + detail
                + " Open Admin > Automatic free routing for credentials, account confirmations, capabilities and cooldowns. No paid fallback was enabled.",
                False,
            )
        return tuple(ordered[:12])

    def record_failure(self, settings, model, failure):
        scope = self._scope(settings, model.provider_id)
        status = failure.status_code
        seconds = 60 if status == 429 else 45
        reason = "rate_limit" if status == 429 else "temporary_failure"
        if status in {401, 402}:
            seconds, reason = 3600, "authentication_or_access"
        elif status in {400, 403, 404, 413}:
            scope += ":" + model.model_id
            seconds, reason = 300, "model_or_request_incompatible"
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

    def record_success(self, model):
        self._last_success = model.ref

    async def status(self, settings, *, force=False):
        await self.refresh(settings, force=force)
        rows = []
        for report in self._reports:
            row = dict(report)
            models = [m for m in self._catalog if m.provider_id == row["provider"]]
            row["available_models"] = sum(
                not self.cooldown(settings, m) for m in models
            )
            provider_cooldown = self._cooldowns.get(
                self._scope(settings, row["provider"])
            )
            if provider_cooldown and provider_cooldown["until"] > time():
                row.update(
                    state="COOLDOWN",
                    retry_at=datetime.fromtimestamp(
                        provider_cooldown["until"], UTC
                    ).isoformat(),
                    reason=provider_cooldown["reason"],
                )
            row["model_ids"] = [m.model_id for m in models]
            row["model_details"] = [
                {
                    "id": m.model_id,
                    "context_tokens": m.context,
                    "max_output_tokens": m.output_limit,
                    "vision": m.vision,
                    "price_basis": m.price_basis,
                }
                for m in models
            ]
            rows.append(row)
        return {
            "automatic": settings.auto_free_models,
            "minimum_context_tokens": MIN_CONTEXT_TOKENS,
            "refreshed_at": datetime.fromtimestamp(self._refreshed, UTC).isoformat(),
            "catalog_ttl_seconds": CATALOG_TTL,
            "eligible_models": len(self._catalog),
            "available_models": sum(r["available_models"] for r in rows),
            "last_success": self._last_success,
            "providers": rows,
            "note": "Eligible means pricing/account and catalog checks passed; it is not a successful inference guarantee. Account-dependent tiers rely on your acknowledgment that paid billing is disabled.",
        }
