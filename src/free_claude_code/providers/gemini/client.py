"""Google AI Studio Gemini provider (OpenAI-compatible chat completions)."""

import asyncio

import httpx

from free_claude_code.core.anthropic import ReasoningReplayMode
from free_claude_code.providers.admission import ProviderAdmissionController
from free_claude_code.providers.base import ProviderConfig
from free_claude_code.providers.gemini.catalog import read_native_catalog
from free_claude_code.providers.google_openai import (
    GeminiReasoningEncoder,
    GoogleOpenAIProvider,
    validate_google_extra_body,
)
from free_claude_code.providers.openai_chat import (
    OpenAIAsyncCredentialProvider,
    OpenAIChatProfile,
    OpenAIChatRequestPolicy,
)

_REQUEST_POLICY = OpenAIChatRequestPolicy(
    provider_name="GEMINI",
    reasoning_replay=ReasoningReplayMode.REASONING_CONTENT,
    include_extra_body=True,
    extra_body_validator=validate_google_extra_body,
    # Google's OpenAI-compatible endpoint rejects the whole request with
    # "Unknown name 'metadata': Cannot find field" if this key is present,
    # whether at the top level or inside a caller-supplied extra_body (#1548).
    unsupported_body_keys=frozenset({"metadata"}),
)
_PROFILE = OpenAIChatProfile(
    _REQUEST_POLICY,
    GeminiReasoningEncoder(),
)


class GeminiProvider(GoogleOpenAIProvider):
    """Gemini API using ``https://generativelanguage.googleapis.com/v1beta/openai/``."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        admission: ProviderAdmissionController,
        api_key_provider: OpenAIAsyncCredentialProvider | None = None,
        default_headers: dict[str, str] | None = None,
    ):
        super().__init__(
            config,
            profile=_PROFILE,
            admission=admission,
            api_key_provider=api_key_provider,
            default_headers=default_headers,
        )
        self._catalog_proxy = config.proxy

    async def list_model_infos(self):
        # Google's native catalog supplies context limits and works independently
        # of the optional OpenAI-compatible /models endpoint.
        async with (
            asyncio.timeout(25),
            httpx.AsyncClient(
                timeout=15,
                trust_env=False,
                follow_redirects=False,
                proxy=self._catalog_proxy,
            ) as client,
        ):
            return await read_native_catalog(client, {"x-goog-api-key": self._api_key})
