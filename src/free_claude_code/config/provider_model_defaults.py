"""Exact-model primary documentation fallbacks; never manufacture catalog IDs."""

from collections.abc import Mapping

# Reviewed 2026-09-20: https://api.atria-asi.ai/docs
# This supplements a live catalog row only. Unknown versions remain unknown.
DOCUMENTED_MODEL_DEFAULTS = {
    "atria": {
        "Atria-Dawn-Preview": {
            "limit": {"context": 256000, "output": 65536},
            "tool_call": True,
            "modalities": {"input": ["text"]},
        },
    },
}


def atria_context_window(row: object) -> int | None:
    if not isinstance(row, Mapping):
        return None
    primary = row.get("context_length")
    if isinstance(primary, int) and not isinstance(primary, bool) and primary > 0:
        return primary
    model_id = row.get("id")
    if not isinstance(model_id, str):
        return None
    return (
        DOCUMENTED_MODEL_DEFAULTS["atria"]
        .get(model_id, {})
        .get("limit", {})
        .get("context")
    )
