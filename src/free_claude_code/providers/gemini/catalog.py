"""Bounded native Gemini model discovery using header-only API-key authentication."""

import json

from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.core.google_errors import GoogleAccessError, google_access_message
from free_claude_code.providers.model_listing import ModelListResponseError

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


async def read_native_catalog(client, headers):
    infos = {}
    seen = set()
    page = None
    for _ in range(20):
        params = {"pageSize": 100}
        if page:
            params["pageToken"] = page
        async with client.stream(
            "GET", MODELS_URL, params=params, headers=headers
        ) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 4 * 1024 * 1024:
                    raise ModelListResponseError(
                        "Google model catalog exceeds the size limit"
                    )
            try:
                payload = json.loads(raw)
            except ValueError:
                response.raise_for_status()
                raise ModelListResponseError(
                    "Google returned an invalid model catalog"
                ) from None
            if response.is_error and (message := google_access_message(payload)):
                raise GoogleAccessError(message)
            response.raise_for_status()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ModelListResponseError("Google returned an invalid model catalog")
        for row in payload["models"]:
            if not isinstance(row, dict) or "generateContent" not in row.get(
                "supportedGenerationMethods", []
            ):
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name.startswith("models/"):
                continue
            context, output = row.get("inputTokenLimit"), row.get("outputTokenLimit")
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
            raise ModelListResponseError("Google model catalog repeated a page token")
        seen.add(page)
    raise ModelListResponseError("Google model catalog exceeds the page limit")
