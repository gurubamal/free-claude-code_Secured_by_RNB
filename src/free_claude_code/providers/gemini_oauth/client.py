"""Renewable OAuth bearer authentication for the public Gemini API."""

import json

import httpx

from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.config.provider_catalog import GEMINI_DEFAULT_BASE
from free_claude_code.providers.gemini.client import GeminiProvider
from free_claude_code.providers.model_listing import ModelListResponseError

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiOAuthProvider(GeminiProvider):
    def __init__(self, config, *, auth, project_id, admission):
        # There is no configurable OAuth token destination.
        if config.base_url != GEMINI_DEFAULT_BASE:
            raise ValueError("Google OAuth requires the official Gemini API endpoint")
        self._auth = auth
        self._project_id = project_id
        self._models_client = httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        )
        super().__init__(
            config,
            admission=admission,
            api_key_provider=self._access_token,
            default_headers={"x-goog-user-project": project_id},
        )

    async def _access_token(self):
        return await self._auth.access_token(project_id=self._project_id)

    async def authorization_headers(self):
        return {
            "Authorization": "Bearer " + await self._access_token(),
            "x-goog-user-project": self._project_id,
        }

    async def list_model_infos(self):
        infos = {}
        seen = set()
        page = None
        for _ in range(20):
            params = {"pageSize": 100}
            if page:
                params["pageToken"] = page
            headers = await self.authorization_headers()
            async with self._models_client.stream(
                "GET", MODELS_URL, params=params, headers=headers
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 4 * 1024 * 1024:
                        raise ModelListResponseError(
                            "Google model catalog exceeds the size limit"
                        )
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("models"), list
            ):
                raise ModelListResponseError("Google returned an invalid model catalog")
            for row in payload["models"]:
                if not isinstance(row, dict) or "generateContent" not in row.get(
                    "supportedGenerationMethods", []
                ):
                    continue
                name = row.get("name")
                if not isinstance(name, str) or not name.startswith("models/"):
                    continue
                context, output = (
                    row.get("inputTokenLimit"),
                    row.get("outputTokenLimit"),
                )
                infos[name] = ProviderModelInfo(
                    model_id=name.removeprefix("models/"),
                    context_window_tokens=context
                    if type(context) is int and context > 0
                    else None,
                    max_output_tokens=output
                    if type(output) is int and output > 0
                    else None,
                )
            page = payload.get("nextPageToken")
            if not page:
                return frozenset(infos.values())
            if not isinstance(page, str) or page in seen:
                raise ModelListResponseError(
                    "Google model catalog repeated a page token"
                )
            seen.add(page)
        raise ModelListResponseError("Google model catalog exceeds the page limit")

    async def cleanup(self):
        try:
            await super().cleanup()
        finally:
            await self._models_client.aclose()
