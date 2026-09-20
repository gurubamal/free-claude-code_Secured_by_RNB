"""Private, credential-scoped inference receipts; catalog discovery is not health."""

import json
from contextlib import suppress
from datetime import UTC, datetime
from time import time

from free_claude_code.config.paths import config_dir_path
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
)

VERIFIED_TTL_SECONDS = 900
HISTORY_TTL_SECONDS = 7 * 86400


class RouteHealth:
    def __init__(self):
        self.path = config_dir_path() / "routing-health.json"
        self.records = {}
        try:
            data = json.loads(read_private_text(self.path))
            if isinstance(data, dict):
                self.records = {
                    key: value
                    for key, value in data.items()
                    if isinstance(key, str)
                    and isinstance(value, dict)
                    and value.get("state") in {"succeeded", "failed"}
                    and isinstance(value.get("checked_at"), (int, float))
                    and time() - HISTORY_TTL_SECONDS < value["checked_at"] <= time()
                }
        except OSError, ValueError:
            pass

    def record(self, key, *, success, status_code=None):
        self.records[key] = {
            "state": "succeeded" if success else "failed",
            "checked_at": time(),
            "status_code": status_code,
        }
        self.records = dict(
            sorted(
                (
                    (k, v)
                    for k, v in self.records.items()
                    if v["checked_at"] > time() - HISTORY_TTL_SECONDS
                ),
                key=lambda pair: pair[1]["checked_at"],
                reverse=True,
            )[:4096]
        )
        with suppress(OSError):
            atomic_write_private_text(self.path, json.dumps(self.records))

    def status(self, key, *, cooldown=None):
        entry = self.records.get(key)
        if cooldown:
            state = "FAILED"
        elif entry is None:
            state = "UNTESTED"
        elif entry["state"] == "failed":
            state = "RECHECK_DUE"
        elif 0 <= time() - entry["checked_at"] <= VERIFIED_TTL_SECONDS:
            state = "VERIFIED"
        else:
            state = "STALE"
        return {
            "state": state,
            "checked_at": datetime.fromtimestamp(entry["checked_at"], UTC).isoformat()
            if entry
            else None,
            "status_code": entry.get("status_code") if entry else None,
            "retry_at": datetime.fromtimestamp(cooldown["until"], UTC).isoformat()
            if cooldown
            else None,
        }
