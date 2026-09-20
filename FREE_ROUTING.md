# Automatic free routing

Policy updated 2026-09-20. The user-requested minimum is **512,000 context tokens** (decimal 512k). This applies to every primary and fallback model, including local models. A large context window is not a daily token allowance, an output allowance, or evidence of good coding quality.

## Setup

1. Keep `AUTO_FREE_MODELS=true` in Admin.
2. Add and save your own provider keys in **Providers**. Do not paste keys into issues or chat.
3. Open **Automatic free routing** (`/admin/free`). For account-dependent tiers, confirm that the saved key belongs to a free account with paid billing disabled.
4. Refresh catalogs. Eligible models require free access, known tool support and at least 512k context. A provider can have a valid free account and still have no qualifying models.

Acknowledgments bind to the credential fingerprint; changing the key requires a new acknowledgment. Revoke it before enabling paid billing. The app cannot independently prove these account billing settings. Saving a key does not purchase credits, upgrade a plan or create an account.

## Provider policies

These are discovery integrations, not a claim that all providers are configured, quota-available or have a qualifying model today.

| Provider | Admission basis | Primary reference |
| --- | --- | --- |
| OpenRouter | All published token prices and surcharges zero; additional request-level zero-price ceilings | [Limits](https://openrouter.ai/docs/api/reference/limits), [model catalog](https://openrouter.ai/api/v1/models) |
| NVIDIA NIM | Saved key plus free-account / no-paid-billing acknowledgment | [API quickstart](https://docs.api.nvidia.com/nim/docs/api-quickstart) |
| Groq | Saved key plus free-account / no-paid-billing acknowledgment | [Rate limits](https://console.groq.com/docs/rate-limits) |
| Cerebras | Saved key plus free-account / no-paid-billing acknowledgment | [Rate limits](https://inference-docs.cerebras.ai/support/rate-limits) |
| Gemini | Saved key plus free-account / no-paid-billing acknowledgment | [Billing](https://ai.google.dev/gemini-api/docs/billing) |
| Mistral | Saved key plus free-account / no-paid-billing acknowledgment | [Free mode and API keys](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key) |
| Kilo | Explicit model with all published prices zero; opaque `kilo-auto/free` excluded under the context requirement | [Gateway](https://kilo.ai/docs/gateway) |
| OpenCode Zen | Intersection of live catalog and primary documentation's free pricing + Chat endpoint tables | [Zen](https://opencode.ai/docs/zen/) |
| ZenMux | All published prompt/completion pricing tiers and surcharges zero | [Pricing](https://zenmux.ai/docs/about/pricing-and-cost.html) |
| SiliconFlow | Explicit zero prices required in catalog; absent pricing means excluded | [Models API](https://docs.siliconflow.com/en/api-reference/models/get-model-list) |
| Ollama | Installed local tool-capable model, explicit configured context; cloud models excluded | [Model information](https://docs.ollama.com/api/show) |
| LM Studio | Loopback catalog explicitly supplies tool support and loaded context | [REST API](https://lmstudio.ai/docs/developer/rest/list) |
| llama.cpp | Loopback catalog explicitly supplies tool support and context | [Server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server) |

Discovery uses current provider catalogs. Where they omit capabilities, the [models.dev registry](https://models.dev/) is a secondary source for tool support, modality and limits; it never independently authorizes free billing. Models missing required capability evidence are excluded. Primary provider limits take precedence. The UI shows model IDs and context sizes, but eligibility remains a catalog assessment rather than a successful inference test.

Local discovery does not install, download or load a new model. A server exposing only a model name without sufficient capability/context metadata may show no eligible models. Ollama discovery examines at most 16 installed entries and marks larger inventories partial. Empty catalogs, unavailable services and incomplete pagination are visible.

## Selection and failures

- Catalog cache: five minutes; key or account-acknowledgment changes invalidate it. Explicit refresh does not reset provider quota.
- Bounded discovery: eight pages per catalog, 16 MiB per response, 25 seconds per provider, four concurrent provider discovery operations. Catalog metadata has a 12-second fetch deadline.
- Request fit: conservative UTF-8 byte estimate plus capped output; no history truncation. A provider tokenizer can still reject a request. Such a failure is handled before output when possible.
- Candidate order: last successful eligible model first; then independent providers before additional sibling models, using context and a coding-name heuristic. This is not benchmarked model ranking.
- At most 12 candidates per request. Messages/Responses use a single provider admission attempt; Chat has one send per candidate. The gateway limits upstream reads/progress; provider startup and cleanup have separate runtime budgets.
- HTTP 429 creates a provider cooldown; authentication/billing failures cool the provider longer; model access/request incompatibility creates a model-specific cooldown. Upstream retry hints can extend the default delay. Reported OpenRouter daily exhaustion disables all its models until the reported reset. A read-only OpenRouter key check also detects zero remaining daily requests before generation. If no reset was reported, the gateway checks again after five minutes; that is a retry check, not a claimed quota reset.
- Cooldowns persist across daemon restarts and are scoped to the provider credential. Changing a key is not a way to reset an account quota.
- Fallback stops as soon as output has been emitted. Interrupted streams return an error, without replaying a possibly executed tool call. Clients must preserve sessions and resume after capacity returns.

The gateway alias remains `open_router/openrouter/free` so existing launchers keep working. It is no longer a promise to call OpenRouter's opaque free router. Client model names and manual fallback lists do not override automatic selection. Responses supports streaming only; the other two interfaces support their documented stream/non-stream modes.

## Limits

The proxy cannot create unlimited free capacity. All qualifying providers may be exhausted or unavailable at once. The 512k minimum intentionally excludes otherwise usable smaller models. The app does not silently relax it.

OpenRouter supports a per-request price ceiling. Other providers rely on current published pricing or your account-billing acknowledgment; pricing/tier changes between checks remain a risk. Hosted providers receive the prompts and code routed to them under their respective data policies. No API key, account response or prompt is included in the public validation artifacts.
