"""Allow account/catalog checks without enabling unreviewed free inference."""

from free_claude_code.application.errors import ApplicationUnavailableError
from free_claude_code.providers.base import BaseProvider, ProviderConfig


class DiscoveryOnlyProvider(BaseProvider):
    def __init__(self, config: ProviderConfig, provider: BaseProvider):
        super().__init__(config)
        self._provider = provider

    async def cleanup(self):
        await self._provider.cleanup()

    async def list_model_infos(self):
        return await self._provider.list_model_infos()

    def stream_messages(self, *args, **kwargs):
        raise ApplicationUnavailableError(
            "This provider supports catalog discovery but is not included in automatic free routing."
        )

    def stream_responses(self, *args, **kwargs):
        raise ApplicationUnavailableError(
            "This provider supports catalog discovery but is not included in automatic free routing."
        )
