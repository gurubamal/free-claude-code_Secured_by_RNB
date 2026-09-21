# Admin setup in pictures

Open the running app at <http://127.0.0.1:8082/admin>. For installation, launch commands and password recovery, see the [main README](../README.md#start-on-windows).

These three screenshots were supplied by the maintainer on **2026-09-20** and are included unchanged. They show the Providers page before the latest routing controls. Counts and configuration states are historical snapshots, not live availability or successful inference checks. Provider names and logos identify their respective services and do not imply endorsement.

## Maximum reasoning across clients

Open **Reasoning** in Admin and set **Reasoning Policy** (root) to **Max**. Set Fable, Opus, Sonnet and Haiku to **Max** or **Inherit**, then save. The root choice applies across automatic routing and fallback for all three API interfaces. Lower effort sent by a client cannot override a fixed root policy. Restart a Claude CLI launched through this app to inherit the updated effort display.

Max uses each adapter's supported controls; Inception maps it to `high`. Providers without a supported control keep their defaults. This setting does not change billing permissions, the 256k context minimum, quotas or output limits. Higher reasoning effort can increase token use and latency. See the [reasoning policy](../FREE_ROUTING.md#reasoning-policy).

## 1. Connections and configured providers

![Admin overview with GitHub Copilot and OpenAI ChatGPT connection cards, a configured OpenRouter card, and cloud-provider setup buttons](images/admin-providers-overview.png)

**Providers** contains account connections and API-key configuration. Use **Configure** to enter a provider's settings or **Edit** to update saved settings. Enter credentials only in the protected local Admin page.

The screenshot's **378 models available** is the OpenRouter provider catalog count. Automatic free routing applies additional pricing/account, tool-support, context and quota checks. Every selected model and fallback must have at least **256,000 context tokens** and enough room for the request; a catalog count does not establish any of those checks.

**OpenAI / ChatGPT** uses the connected-account flow. Enable **Allow connected subscriptions** in Routing controls to permit eligible subscription models. Connecting alone does not enable billing or add free capacity. A model with 272k default context now meets the floor. Experimental larger maxima are not used to admit oversized requests.

## 2. More cloud-provider settings

**Gemini / Google account** now appears under **OAuth providers**. Choose **Edit** to save your own Google Desktop OAuth client ID, secret and Cloud project ID; then choose **Sign in with Google**. Enable **Allow paid API routes** to include it in automatic routing. Google API quotas/billing apply, and the 256k floor remains. See the [Google account setup guide](GOOGLE_ACCOUNT.md) for the required one-time Google configuration. The historical screenshots below predate this addition.

![Cloud-provider cards including Gemini, Groq, Kilo, Mistral and NVIDIA NIM, each with a Configure button](images/admin-cloud-providers.png)

Scroll through the cloud providers to find the service whose credentials you want to configure. A visible card means a configuration interface exists; it does not mean that provider is configured, included in the automatic free pool, or currently offers a qualifying model.

**Atria** and **Inception** now have their own **Configure** cards and masked API-key fields. Get an Atria key from [its console](https://api.atria-asi.ai/console), or an Inception key from [its platform](https://platform.inceptionlabs.ai/). Save it in Admin and keep repository files free of credentials. Both integrations require the paid-API switch for routing consideration. The documented Atria Dawn Preview limit is 256k and Inception Mercury 2.5 is 260k. Both meet the floor adopted on 2026-09-21; routing still requires paid-API permission, a request that fits and available account capacity. Inception's public catalog is not an authentication check, and key saving does not send a billable test request.

For account-dependent free tiers, save the key, then open **Routing controls** in the sidebar. Confirm that the key belongs to a free account with paid billing disabled. The confirmation binds to that key and must be repeated after a key change. The app relies on this statement for account billing. Without it, these keys are used only as paid access when enabled.

## 3. Remaining cloud providers and local servers

![Additional cloud-provider cards including OpenCode Zen, SiliconFlow and ZenMux, followed by the Local providers section](images/admin-cloud-and-local-providers.png)

The lower part of the page includes more cloud providers and the **Local providers** section. Local discovery uses an existing server and installed models; it does not download or load models for you. The same tool-support, context-capacity and request-fit checks apply to local automatic candidates.

## Check readiness and start coding

The **Verified free failovers** panel is the server's live guide to recently
working free models. Green requires completed inference with output in the last
15 minutes. Red means failed/blocked; amber means untested, expired or due for a
recheck. Open **Show models** for per-model timestamps and retry details. Health
updates every ten seconds while visible and receipts survive server restart.
Catalog refresh alone does not test inference. See the
[Free provider guide](FREE_PROVIDER_GUIDE.md) for admission rules and examples.

1. Save the settings for providers you intend to use.
2. Open **Routing controls** and complete required free-account confirmations. Optional switches permit subscriptions and paid APIs. Choose billing priority and the preferred free-model order (DeepSeek V4.1 Flash, Kimi K3, Qwen 3.8 Max, GLM 5.3 Flash by default). Availability appears beside each preference. Move providers up/down within those tiers, and uncheck unwanted providers; save preferences. Paid APIs may charge money and subscriptions may consume credits. Set limits with the provider; this gateway has no monetary budget cap.
3. Select **Refresh catalogs**. Review missing credentials, catalog failures, eligible models, context and cooldowns. Refreshing does not reset quota.
4. Optionally use **Choose provider and model**: choose Free models, a provider and a model, then **Use as first preference**. Automatic fallback stays enabled when that route is exhausted or unavailable. **Return to automatic selection** clears the preference. The selected preference and actual gateway route are shown separately.
5. Launch Claude Code using `Claude-Free.ps1` from the repository, or invoke that launcher by its full path from your coding project. Automatic mode selects an eligible model without a manual model choice.

The CLI equivalents are `Run-Hardened.ps1 route list`, `route use PROVIDER [MODEL]`, `route status` and `route auto`. Selection is shared by new requests from all gateway clients and persists after restart.

The **Actual gateway route** panel displays the provider/model that completed the last successful request and the latest attempt, with billing category and timestamps. It updates every ten seconds while visible. Records reset on restart and cover all connected clients. A pending attempt is not a successful response; a CLI header may remain a fixed alias.

![Routing controls with synthetic route identity, paid switches and provider priority](../security-validation/free-routing.png)

This fourth image is a **synthetic browser test**, distinct from the three supplied screenshots. It illustrates the current controls and is not a record of the maintainer's provider credentials or live route.

Routing can have no available candidates even after a successful connection and catalog load. Eligibility is a catalog/account assessment, not proof of inference. If every eligible route is unavailable, automatic recovery waits within its deadline before returning an error; it retains the selected billing policy and 256k minimum.

See [Automatic routing](../FREE_ROUTING.md) for provider and interface limits, [HARDENING.md](../HARDENING.md) for storage/access controls, and [VALIDATION.md](../VALIDATION.md) for dated checks.

## Gemini catalog access errors

Gemini API-key discovery uses Google's native model endpoint, with header authentication and bounded pagination. The provider card and Routing controls display recognized Google error codes with fixed instructions; raw upstream messages, credentials and project identifiers are not returned to the page.

- **CONSUMER_SUSPENDED:** Google has suspended the Cloud project associated with the key. Review the project's notification, resolve the stated issue and use Google's appeal/support process. A replacement key on the same project does not lift the suspension. [Google recovery guidance](https://docs.cloud.google.com/resource-manager/docs/project-suspension-guidelines).
- **SERVICE_DISABLED:** enable the Generative Language API for the intended project in Google Cloud.
- **API_KEY_INVALID:** replace the key using the protected Gemini **Edit** form with a valid Gemini API key from Google AI Studio.

After resolving access, refresh catalogs. Eligible alternatives remain available to automatic routing while Gemini is excluded. A failure does not enable paid billing or waive the 256k minimum. Google consumer Pro/Ultra access and Gemini Developer API project access remain separate.
