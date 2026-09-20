# Automatic routing: free first, optional paid fallback

Policy updated 2026-09-20. The minimum is **512,000 context tokens** (decimal 512k) for every primary and fallback, including paid, subscription and local models. An experimental maximum does not replace the provider's active default limit. Context size is not a daily allowance, output allowance or evidence of coding quality.

## Reasoning policy

Set **Admin → Reasoning → Reasoning Policy** to **Max** to override client effort across Messages, Responses and Chat Completions, including every automatic fallback. Set each Claude route override to **Inherit** or **Max** for consistency when automatic routing is disabled. Fixed configuration also takes precedence over a client's no-thinking model alias and the classifier's usual speed optimization; classifier verdict filtering remains enabled.

The adapter maps Max to its supported API controls. Inception and Gemini receive `high`; DeepSeek receives `max`; OpenRouter and Kilo receive a `reasoning` object with effort `max`. NVIDIA NIM uses the existing thinking template/budget encoder. Providers without an implemented effort control retain their defaults; selecting Max cannot create a reasoning capability. Protocol shapes and output limits are applied separately for each fallback, without carrying provider-specific fields into the next request.

The Claude launcher sets `CLAUDE_CODE_EFFORT_LEVEL` for fixed root effort, so newly launched sessions and their child agents inherit it. Claude's model/organization caps can affect the displayed level; the gateway's configured policy controls its upstream request. Other clients may display their own local effort setting even when the gateway overrides it. See [Claude's effort controls](https://code.claude.com/docs/en/model-config#adjust-effort-level), [OpenRouter reasoning controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), and [Inception reasoning efforts](https://docs.inceptionlabs.ai/capabilities/reasoning-efforts).

Max does not enlarge the current 8,192-token automatic output ceiling or consume the whole context window as reasoning. Reasoning and visible output can share that ceiling. Provider quotas, paid permissions, the 512k floor, tool support and request-fit checks still apply. A higher effort request is not proof of a particular amount of internal computation. The shipped root default remains **Client**; Max is an explicit saved preference.

## Setup

Use the [Free provider guide](docs/FREE_PROVIDER_GUIDE.md) to configure free-first
routing and interpret the live **Verified free failovers** list. Completed
inference with output earns green for 15 minutes. Active cooldowns are red;
untested, stale and recheck-due routes are amber. Private credential-scoped
receipts survive restart; catalog refresh alone never marks a model green.

For a visual walkthrough of provider configuration, see the [Admin screenshot guide](docs/ADMIN_GUIDE.md). Its provider catalog counts are separate from the eligible-model counts on the automatic routing page.

1. Keep `AUTO_FREE_MODELS=true` in Admin.
2. Add and save your own provider keys in **Providers**. Do not paste keys into issues or chat.
3. Open **Routing controls** (`/admin/free`). For account-dependent free tiers, confirm that the saved key belongs to a free account with paid billing disabled.
4. Optionally enable **Allow connected subscriptions** and/or **Allow paid API routes**. Both default off. Choose the category order, move providers up/down, exclude unwanted providers and **Save routing preferences**.
5. Refresh catalogs and review eligibility. Known tool support, 512k+ context and request fit are required. A valid account may still have no eligible or available models.
6. Use **Actual gateway route** to see the real provider/model, billing category, context, latest attempt and last successful completion. It updates every ten seconds while visible. Records are in memory and reset on restart; an interrupted connection can leave an unfinished latest attempt. CLI model labels may show a fixed alias.

Acknowledgments bind to the key fingerprint; changing the key requires a new acknowledgment. Revoke it before enabling paid billing. An unconfirmed account-dependent key is considered only as paid access when that category is enabled. The app cannot independently prove these billing settings. Saving a key does not purchase credits, upgrade a plan or create an account. **There is no monetary budget cap in this gateway**; use each provider's spending controls. Subscription access can consume account allowance or purchased credits.

## Manual first preference with automatic fallback

Use **Choose provider and model** on `/admin/free`, or `Run-Hardened.ps1 route use PROVIDER [MODEL]`. Choose exact IDs from `route list` (free by default). Omitting MODEL chooses the best eligible model in that provider. `route status` shows the preference and last successful route; `route auto` restores the saved automatic order. The web console offers the same automatic-return button.

A selected route is tried before the normal category/model/provider priorities, but **automatic fallback always remains on**. It cannot enable paid access, undo provider exclusions, waive context/capability checks, reset cooldowns or force an unknown catalog ID. If unavailable or unsuitable, automatic candidates take over; after cooldown, the saved preference can be first again. Selecting a model currently cooling down is allowed, with its unavailability displayed. No route can guarantee capacity when every candidate is exhausted.

The preference is stored outside the repository and applies to new requests from all clients. Existing streams are not replayed. Choose `--billing paid_api` or `--billing subscription` only for categories already enabled in Admin. Free selection does not disable previously authorized paid fallback: fallback follows the saved billing switches/order.

The local CLI calls a restricted selection endpoint using the proxy token loaded from protected storage. It requires a loopback client/Host, a control header and no browser Origin/Fetch-Metadata. It cannot edit keys or billing switches. Web changes retain the Admin session and CSRF checks. Anyone with local proxy-token access can change this shared route preference; use a separate gateway when clients need independent policy.

## Provider policies

These are discovery integrations, not a claim that all providers are configured, quota-available or have a qualifying model today.

Providers outside these tables remain outside automatic routing. Catalog visibility alone does not authorize generation. The following hosted policies may also supply paid candidates when explicitly enabled; free admission remains separately checked.

| Provider | Admission basis | Primary reference |
| --- | --- | --- |
| OpenRouter | All published prices zero; request-level zero-price ceilings. In mixed mode, explicit `:free` models form the free group. Other models require paid opt-in. | [Limits](https://openrouter.ai/docs/api/reference/limits), [model catalog](https://openrouter.ai/api/v1/models) |
| NVIDIA NIM | Saved key plus free-account / no-paid-billing acknowledgment | [API quickstart](https://docs.api.nvidia.com/nim/docs/api-quickstart) |
| Groq | Saved key plus free-account / no-paid-billing acknowledgment | [Rate limits](https://console.groq.com/docs/rate-limits) |
| Cerebras | Saved key plus free-account / no-paid-billing acknowledgment | [Rate limits](https://inference-docs.cerebras.ai/support/rate-limits) |
| Gemini | Saved key plus free-account / no-paid-billing acknowledgment | [Billing](https://ai.google.dev/gemini-api/docs/billing) |
| Mistral | Saved key plus free-account / no-paid-billing acknowledgment | [Free mode and API keys](https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key) |
| Kilo | Explicit model with all published prices zero; opaque `kilo-auto/free` excluded under the context requirement | [Gateway](https://kilo.ai/docs/gateway) |
| OpenCode Zen | Intersection of live catalog and primary documentation's free pricing + Chat endpoint tables | [Zen](https://opencode.ai/docs/zen/) |
| ZenMux | All published prompt/completion pricing tiers and surcharges zero | [Pricing](https://zenmux.ai/docs/about/pricing-and-cost.html) |
| SiliconFlow | Explicit zero catalog prices for free admission; unknown/nonzero prices require paid opt-in | [Models API](https://docs.siliconflow.com/en/api-reference/models/get-model-list) |
| Ollama | Installed local tool-capable model, explicit configured context; cloud models excluded | [Model information](https://docs.ollama.com/api/show) |
| LM Studio | Loopback catalog explicitly supplies tool support and loaded context | [REST API](https://lmstudio.ai/docs/developer/rest/list) |
| llama.cpp | Loopback catalog explicitly supplies tool support and context | [Server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server) |

| Additional provider | Required opt-in and interface scope | Primary reference |
| --- | --- | --- |
| DeepSeek | Paid API; existing native adapter in automatic selection | [DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/) |
| Atria | Paid-API permission required; `https://api.atria-asi.ai/v1`; exact `Atria-Dawn-Preview` ID. Documented 256k context is below the 512k floor. | [Atria API](https://api.atria-asi.ai/docs) |
| Inception | Paid-API permission required; `https://api.inceptionlabs.ai/v1`; chat-only discovery at `/chat/completions/models`. Mercury 2.5 is 260k, Mercury 2 is 128k; both excluded by the 512k floor. | [Models](https://docs.inceptionlabs.ai/get-started/models), [chat catalog](https://docs.inceptionlabs.ai/api-reference/models/list-chat-models) |
| Gemini / Google account | Paid-API opt-in; your own Desktop OAuth client and Cloud project; Messages/Responses/Chat, renewable browser login; same 512k floor | [OAuth setup](docs/GOOGLE_ACCOUNT.md), [Google OAuth](https://ai.google.dev/gemini-api/docs/oauth) |
| Cline API / ClinePass | Paid API; API key and documented `/models` + Chat endpoints; internal provider ID remains `cline_pass` | [Cline API](https://docs.cline.bot/api/overview) |
| Command Code | Paid API; `https://api.commandcode.ai/provider/v1`; only catalog entries explicitly supporting `/chat/completions`. Anthropic-only entries are excluded. | [Provider API](https://commandcode.ai/blog/command-code-provider-api) |
| Kimchi | Paid API; `https://llm.kimchi.dev/openai/v1`; missing context/tool metadata excludes models | [Quickstart](https://docs.kimchi.dev/docs/inference-quickstart) |
| OpenAI / ChatGPT | Connected subscription; Messages/Responses only; primary default context must meet 512k | [Authentication](https://learn.chatgpt.com/docs/auth) |
| GitHub Copilot | Connected subscription; Messages/Responses only; same tool/context checks | [Copilot documentation](https://docs.github.com/en/copilot) |

These additional API-key integrations use the paid switch even when a plan includes credits; connecting these accounts does not establish free capacity. Cline's catalog may omit capabilities, requiring matching registry metadata. Cline and Command Code can use exact model-ID matches from the OpenRouter registry for missing capabilities and limits; primary gateway context takes precedence. These integrations do not install the providers' native coding harnesses.

Atria and Inception were reviewed on 2026-09-20. Atria supplements only the exact live `Atria-Dawn-Preview` catalog row with primary documented limits (256,000 context / 65,536 output), text input and tool support when missing; unknown versions receive no inferred limits. Inception reads primary `context_length`, `max_output_length`, `input_modalities` and `supported_features` from its Chat catalog, excluding FIM/edit-only models by using the dedicated endpoint. Its adapter uses standard append-only SSE, not diffusion visualization, and maps reasoning to the documented `instant`/`low`/`medium`/`high` values. Current models from both providers fail the 512k minimum; the routing report counts those exclusions. Future live entries must independently meet the same checks. No authenticated inference was performed for this integration.

Inception's model endpoint returned a catalog without authentication during review despite the documentation's Bearer requirement; it cannot validate an API key. Atria lacks a reviewed, documented read-only verification contract. Both therefore allow secure key saving while explicitly leaving key verification unconfirmed. No generation is sent merely to save a key.

Discovery uses current provider catalogs. Where they omit capabilities, the [models.dev registry](https://models.dev/) is a secondary source for tool support, modality and limits; it never independently authorizes free billing. Models missing required capability evidence are excluded. Primary provider limits take precedence. The UI shows model IDs and context sizes, but eligibility remains a catalog assessment rather than a successful inference test.

Local discovery does not install, download or load a new model. A server exposing only a model name without sufficient capability/context metadata may show no eligible models. Ollama discovery examines at most 16 installed entries and marks larger inventories partial. Empty catalogs, unavailable services and incomplete pagination are visible.

## Selection and failures

Google OAuth uses the public Gemini API with its native, authenticated model catalog. The input-token limit comes from Google; exact model-ID registry matches supply omitted tool metadata. Native discovery is limited to 20 pages and 4 MiB per page, with the pool's 25-second provider deadline still applying. Login alone does not establish free billing, so this route requires the paid-API switch even when the chosen project has free quota. It does not use Gemini CLI tokens or consumer-subscription allowances.

Saving Cline or Command Code credentials is separate from verifying them. Their public catalogs cannot prove key validity; Admin now explains that a real inference request is needed when no supported read-only credential probe exists.

- Catalog cache: five minutes; credentials, account acknowledgments and billing-switch changes invalidate it. Explicit refresh does not reset quota.
- Bounded discovery: eight pages per catalog, 16 MiB per response, 25 seconds per provider, four concurrent provider discovery operations. Catalog metadata has a 12-second fetch deadline.
- Request fit: conservative UTF-8 byte estimate plus capped output; no history truncation. A provider tokenizer can still reject a request. Such a failure is handled before output when possible.
- Candidate order: saved manual choice first when eligible, then saved billing categories. Within each category, recently verified routes precede untested/stale routes and previously failed routes due for recheck. Within each health tier, free-family order is **DeepSeek V4.1 Flash → Kimi K3 → Qwen 3.8 Max → GLM 5.3 Flash**, across providers, then other eligible free models. Provider order and last success break remaining ties. Cooldown expiry permits a recheck; it does not prove recovery. This is a configured availability policy, not a benchmarked ranking.
- Edit the comma-separated family IDs in Routing controls: `deepseek-v4.1-flash,kimi-k3,qwen3.8-max,glm-5.3-flash`. An empty UI list saves `FREE_MODEL_PRIORITY=none` to disable family preferences. Omitted API fields preserve existing preferences. Status shows free eligibility and current availability for each family.
- Family matching accepts explicit versions, provider namespaces, dated releases and `:free` suffixes. It does not equate DeepSeek V4 with V4.1, FlashX with Flash, or opaque/latest aliases with a fixed version. Live catalog pricing, account confirmations, tool support, request fit and the 512k floor remain mandatory. A preferred model may currently have only paid routes; no free route is manufactured.
- At most 12 candidates per request, reserving room for other providers within each category and one slot for every later eligible category. After a failure, another provider in the next category being considered precedes remaining siblings, without jumping the billing order. Messages/Responses use one provider admission attempt; Chat has one send per candidate. Automatic mode owns cooldowns; the provider's separate recovery circuit is disabled while rate/concurrency admission remains active.
- HTTP 429 creates a provider/billing cooldown; authentication or balance failures cool that scope longer. CommandCode's explicit `upgrade_required` denial blocks its API provider scope for an hour; generic 403 remains model-specific. Model incompatibility and temporary 5xx failures cool the affected model. Upstream retry hints can extend the delay. OpenRouter daily exhaustion blocks its free routes, while a read-only paid-balance check separately skips unfunded paid routes. Without a reported reset, the five-minute check is a retry check, not a promised quota reset.
- Cooldowns persist across daemon restarts and are scoped to the provider credential. Changing a key is not a way to reset an account quota.
- Initial stream headers and heartbeats are buffered until output or completion. Pre-output errors, EOF and timeout can fall back. Fallback stops once text, reasoning or tool output has reached the client; interrupted streams return an error without replaying a possibly executed tool call. Clients must preserve sessions and resume after capacity returns.

The alias remains `open_router/openrouter/free` for launcher compatibility; explicit paid opt-ins may route it to paid models. Client names and manual fallback lists do not override automatic selection. Responses supports streaming only. Chat Completions excludes connected subscriptions and supports API-key/local routes plus Gemini API OAuth; Messages and Responses can also use eligible subscriptions. Existing harnesses remain responsible for tool execution and session persistence.

## Limits

The proxy cannot create unlimited capacity. All qualifying providers can be exhausted or unavailable together. In that case it reports failure rather than looping indefinitely or hiding failed output. The 512k floor intentionally excludes smaller models and is never silently relaxed. A paid switch is permission to use available paid access, not proof of funds or model access.

OpenRouter supports a per-request price ceiling. Other providers rely on current published pricing or your account-billing acknowledgment; pricing/tier changes between checks remain a risk. Hosted providers receive the prompts and code routed to them under their respective data policies. No API key, account response or prompt is included in the public validation artifacts.
