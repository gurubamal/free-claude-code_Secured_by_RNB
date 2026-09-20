"""Version-specific free-model preferences; never infer price or capability."""

import re

FREE_MODEL_FAMILIES = {
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "kimi-k3": "Kimi K3",
    "qwen3.8-max": "Qwen 3.8 Max",
    "glm-5.3-flash": "GLM 5.3 Flash",
}
DEFAULT_FREE_MODEL_PRIORITY = ",".join(FREE_MODEL_FAMILIES)


def preferred_free_families(value: str | None) -> list[str]:
    return [p for p in (value or "").split(",") if p in FREE_MODEL_FAMILIES]


def free_model_family(model_id: str) -> str | None:
    """Match explicit versions and dated releases, not opaque/latest aliases.

    Provider namespaces and an explicit :free suffix do not change identity.
    FlashX, previews, batch routes and other variants are deliberately distinct.
    The live catalog must independently establish free eligibility and context.
    """
    name = model_id.lower().rsplit("/", 1)[-1].removesuffix(":free")
    name = re.sub(r"[-_](?:\d{4}|\d{8}|\d{4}-\d{2}-\d{2})$", "", name)
    normalized = re.sub(r"[-_. ]", "", name)
    for family in FREE_MODEL_FAMILIES:
        aliases = (family, family.replace("deepseek-v", "deepseek-"))
        if normalized in {re.sub(r"[-_. ]", "", alias) for alias in aliases}:
            return family
    return None


def free_preference_rank(model_id: str, priority: list[str]) -> int:
    family = free_model_family(model_id)
    return priority.index(family) if family in priority else len(priority)
