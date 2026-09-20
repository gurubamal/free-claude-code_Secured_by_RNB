"""Reviewed free-route policies. Provider catalogs remain the live authority."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .provider_catalog import PROVIDER_CATALOG


@dataclass(frozen=True)
class FreeProviderPolicy:
    provider_id: str
    mode: str
    metadata_id: str
    source: str


FREE_PROVIDERS = (
    FreeProviderPolicy(
        "open_router",
        "zero_price",
        "openrouter",
        "https://openrouter.ai/docs/api/reference/limits",
    ),
    FreeProviderPolicy(
        "nvidia_nim",
        "free_account",
        "nvidia",
        "https://docs.api.nvidia.com/nim/docs/api-quickstart",
    ),
    FreeProviderPolicy(
        "groq", "free_account", "groq", "https://console.groq.com/docs/rate-limits"
    ),
    FreeProviderPolicy(
        "cerebras",
        "free_account",
        "cerebras",
        "https://inference-docs.cerebras.ai/support/rate-limits",
    ),
    FreeProviderPolicy(
        "gemini",
        "free_account",
        "google",
        "https://ai.google.dev/gemini-api/docs/billing",
    ),
    FreeProviderPolicy(
        "mistral",
        "free_account",
        "mistral",
        "https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key",
    ),
    FreeProviderPolicy("kilo", "zero_price", "kilo", "https://kilo.ai/docs/gateway"),
    FreeProviderPolicy(
        "opencode_zen", "zen_free", "opencode", "https://opencode.ai/docs/zen/"
    ),
    FreeProviderPolicy(
        "zenmux",
        "zero_price",
        "zenmux",
        "https://zenmux.ai/docs/about/pricing-and-cost.html",
    ),
    FreeProviderPolicy(
        "siliconflow",
        "zero_price",
        "siliconflow",
        "https://docs.siliconflow.com/en/api-reference/models/get-model-list",
    ),
    FreeProviderPolicy("ollama", "local", "ollama", "https://docs.ollama.com/api/show"),
    FreeProviderPolicy(
        "lmstudio", "local", "lmstudio", "https://lmstudio.ai/docs/developer/rest/list"
    ),
    FreeProviderPolicy(
        "llamacpp",
        "local",
        "llamacpp",
        "https://github.com/ggml-org/llama.cpp/tree/master/tools/server",
    ),
)
POLICY_BY_ID = {p.provider_id: p for p in FREE_PROVIDERS}


def provider_key(settings, provider_id: str) -> str:
    descriptor = PROVIDER_CATALOG[provider_id]
    return (
        (getattr(settings, descriptor.credential_attr, None) or "")
        if descriptor.credential_attr
        else ""
    )


def zero_number(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)).is_finite() and Decimal(str(value)) == 0
    except InvalidOperation, ValueError:
        return False


def explicitly_zero_priced(row: dict) -> bool:
    """Both token prices and every published surcharge/tier must be zero."""
    prices = row.get("pricing")
    if isinstance(prices, dict):
        if not all(zero_number(prices.get(k)) for k in ("prompt", "completion")):
            return False
        return all(zero_number(v) for v in prices.values())
    tiers = row.get("pricings")
    if not isinstance(tiers, dict) or not all(
        tiers.get(k) for k in ("prompt", "completion")
    ):
        return False
    return all(
        isinstance(items, list)
        and bool(items)
        and all(
            isinstance(item, dict) and zero_number(item.get("value")) for item in items
        )
        for items in tiers.values()
    )


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def zen_free_chat_ids(html: str) -> set[str]:
    """Join the provider's current pricing and endpoint tables, never just a suffix."""
    tables = _Tables()
    tables.feed(html)
    free_names = {
        row[0]
        for row in tables.rows
        if len(row) >= 3
        and row[1:3] == ["Free", "Free"]
        and all(value in {"Free", "-", "—"} for value in row[3:])
    }
    return {
        row[1]
        for row in tables.rows
        if len(row) >= 3
        and row[0] in free_names
        and row[2] == "https://opencode.ai/zen/v1/chat/completions"
    }
