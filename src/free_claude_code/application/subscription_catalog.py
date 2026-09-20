"""Catalog-only access through the existing connected-account runtime."""

import asyncio
import hashlib
from dataclasses import replace

from free_claude_code.config.free_providers import CONNECTED_ROUTING_PROVIDERS


class SubscriptionCatalog:
    def __init__(self, services):
        self.services = services

    async def identities(self):
        result = {}
        for policy in CONNECTED_ROUTING_PROVIDERS:
            try:
                async with asyncio.timeout(25):
                    status = await self.services.admin.connected_account_status(
                        policy.provider_id
                    )
                if status.connected:
                    # Account identity/revision only; never inspect or export access tokens.
                    identity = f"{status.email or status.display_identity or ''}:{status.revision}"
                    result[policy.provider_id] = hashlib.sha256(
                        identity.encode()
                    ).hexdigest()
            except Exception:
                continue
        return result

    async def discover(self, provider_id):
        result = await self.services.admin.test_provider(provider_id)
        if result.get("ok") is not True:
            raise ValueError("Connected account catalog unavailable")
        prefix = provider_id + "/"
        return tuple(
            replace(info, model_id=info.model_id.removeprefix(prefix))
            for info in self.services.requests.cached_prefixed_model_infos()
            if info.model_id.startswith(prefix)
        )
