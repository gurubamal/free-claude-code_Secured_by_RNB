"""Catalog checks must not grant unreviewed providers free inference access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from free_claude_code.application.errors import ApplicationUnavailableError
from free_claude_code.application.model_metadata import ProviderModelInfo
from free_claude_code.config.admin.values import load_config_response
from free_claude_code.config.loader import ManagedConfigStore
from free_claude_code.config.settings import Settings
from free_claude_code.providers.runtime.discovery_only import DiscoveryOnlyProvider
from free_claude_code.providers.runtime.factory import prepare_provider


@pytest.mark.asyncio
async def test_openai_discovery_allowed_but_generation_still_blocked():
    info = ProviderModelInfo("synthetic-openai", context_window_tokens=1048576)
    underlying = SimpleNamespace(
        list_model_infos=AsyncMock(return_value=frozenset({info})),
        cleanup=AsyncMock(),
        stream_messages=Mock(),
        stream_responses=Mock(),
    )
    provider = prepare_provider("openai", {"openai": lambda: lambda *args: underlying})(
        Settings()
    )
    assert isinstance(provider, DiscoveryOnlyProvider)
    assert await provider.list_model_infos() == frozenset({info})
    for operation in (provider.stream_messages, provider.stream_responses):
        with pytest.raises(
            ApplicationUnavailableError, match="not included in automatic free routing"
        ):
            operation(None)
    underlying.stream_messages.assert_not_called()
    underlying.stream_responses.assert_not_called()
    await provider.cleanup()
    underlying.cleanup.assert_awaited_once()


def test_manual_mode_keeps_existing_provider_and_admin_reports_policy():
    underlying = SimpleNamespace()
    provider = prepare_provider("openai", {"openai": lambda: lambda *args: underlying})(
        Settings(auto_free_models=False)
    )
    assert provider is underlying
    store = ManagedConfigStore()
    store.initialize(env={})
    response = load_config_response(store.read(env={}))
    assert response["automatic_free_models"] is True
    policies = {
        p["provider_id"]: p["automatic_free_policy_supported"]
        for p in response["provider_status"]
    }
    assert policies["openai"] is False
    assert policies["open_router"] is True
