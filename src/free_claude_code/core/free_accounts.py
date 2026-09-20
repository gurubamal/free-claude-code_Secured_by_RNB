"""Local, credential-bound acknowledgments for account-dependent free tiers."""

import hashlib
import json
from time import time

from free_claude_code.config.free_providers import POLICY_BY_ID, provider_key
from free_claude_code.config.paths import config_dir_path
from free_claude_code.core.interprocess_lock import InterprocessFileLock
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
)


def credential_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class FreeAccountConfirmations:
    """An operator acknowledgment is not an independent provider billing check."""

    def __init__(self):
        self.path = config_dir_path() / "free-account-confirmations.json"

    def _read(self):
        try:
            value = json.loads(read_private_text(self.path))
            return value if isinstance(value, dict) else {}
        except OSError, ValueError:
            return {}

    def confirmed(self, settings, provider_id):
        entry = self._read().get(provider_id, {})
        key = provider_key(settings, provider_id)
        return (
            bool(key)
            and isinstance(entry, dict)
            and entry.get("credential") == credential_fingerprint(key)
        )

    def set(self, settings, provider_id, confirmed):
        policy = POLICY_BY_ID.get(provider_id)
        if policy is None or policy.mode != "free_account":
            raise ValueError("This provider does not use a free-account acknowledgment")
        key = provider_key(settings, provider_id)
        if confirmed and not key:
            raise ValueError("Save the provider API key in Providers first")
        lock = InterprocessFileLock(self.path.with_suffix(".lock"))
        if not lock.acquire(wait=True, timeout=5):
            raise OSError("Free-account settings are busy; try again")
        try:
            entries = self._read()
            if confirmed:
                entries[provider_id] = {
                    "credential": credential_fingerprint(key),
                    "confirmed_at": time(),
                }
            else:
                entries.pop(provider_id, None)
            atomic_write_private_text(self.path, json.dumps(entries))
        finally:
            lock.release()
