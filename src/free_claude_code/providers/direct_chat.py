"""Reuse provider reasoning encoders for the raw Chat Completions ingress."""

from copy import deepcopy

from free_claude_code.application.reasoning import resolve_chat_reasoning_policy
from free_claude_code.config.settings import Settings
from free_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    apply_openai_chat_body_policy,
)


def prepare_chat_body(settings: Settings, provider_id: str, payload: dict) -> dict:
    """Encode trusted policy without constructing clients or mutating fallback input.

    The HTTP adapter allowlists client fields before invoking this function.
    Only generated provider extensions are flattened from SDK to wire format.
    """
    reasoning = resolve_chat_reasoning_policy(payload, settings.reasoning_policy)
    body = deepcopy(payload)
    body.pop("reasoning_effort", None)
    body.pop("extra_body", None)
    profile = OPENAI_CHAT_PROFILES.get(provider_id)
    if provider_id in {"gemini", "gemini_oauth"}:
        from free_claude_code.providers.gemini.client import _PROFILE

        profile = _PROFILE
    elif provider_id == "open_router":
        from free_claude_code.providers.open_router.client import _PROFILE

        profile = _PROFILE
    elif provider_id == "kilo":
        from free_claude_code.providers.kilo.client import _PROFILE

        profile = _PROFILE
    elif provider_id == "lmstudio":
        from free_claude_code.providers.lmstudio.client import _PROFILE

        profile = _PROFILE

    if profile is not None:
        apply_openai_chat_body_policy(body, profile.request_policy)
        profile.apply_reasoning_to_body(body, reasoning)

    if provider_id == "deepseek":
        from free_claude_code.providers.deepseek.compat import (
            finalize_deepseek_chat_body,
        )

        finalize_deepseek_chat_body(body, reasoning)
    elif provider_id == "mistral":
        from free_claude_code.providers.mistral.reasoning import (
            apply_mistral_reasoning_request_shape,
        )

        apply_mistral_reasoning_request_shape(body, reasoning=reasoning)
    elif provider_id == "nvidia_nim":
        from free_claude_code.providers.nvidia_nim.request_options import (
            apply_nim_request_options,
        )

        apply_nim_request_options(body, reasoning, nim=settings.nim)

    extensions = body.pop("extra_body", None)
    if isinstance(extensions, dict):
        body.update(extensions)
    return body
