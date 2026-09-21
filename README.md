# free-claude-code_Secured_by_RNB

A local gateway for coding assistants, maintained as the **RNB hardening fork**. It routes across configured free providers, with optional subscription and paid-API fallback, recoverable administrator access and protected credential storage on Windows.

This repository builds on an existing MIT-licensed project; the upstream source and license are credited below. This guide describes the RNB version and its changes.

## Documentation

- [README](https://github.com/gurubamal/free-claude-code_Secured_by_RNB/blob/main/README.md): features, setup, paid switches and actual provider/model display.
- [Routing guide](https://github.com/gurubamal/free-claude-code_Secured_by_RNB/blob/main/FREE_ROUTING.md): provider support, priorities, automatic fallback, cooldowns and the 256k minimum.
- [Illustrated Admin guide](https://github.com/gurubamal/free-claude-code_Secured_by_RNB/blob/main/docs/ADMIN_GUIDE.md): the three supplied screenshots and control instructions.
- [Google account setup](docs/GOOGLE_ACCOUNT.md): sign in to the Gemini API with your own Google Desktop OAuth client, without a Gemini API key.

## RNB changes

| Area | Implemented in this fork |
| --- | --- |
| Local access | Loopback-only binding and mandatory proxy authentication |
| Administrator login | Unique temporary password, required first-login change, expiring browser sessions and local recovery |
| Credential storage | Windows user-bound DPAPI encryption for managed provider credentials and Admin state; permanent Admin passwords are hashed |
| Password reset | Invalidates browser sessions while preserving provider configuration |
| Automatic routing | Free by default; opt-in subscriptions and paid APIs, configurable category/provider order, persistent cooldowns and a strict 256k floor |
| Route visibility | Actual provider/model, billing category, latest attempt and last completed success in the web controls |
| Inference health | Green/red/amber model badges, timestamped verified-free guide, private receipts that survive restart, and recently working routes preferred within each billing category |
| Reasoning controls | One root effort policy for Messages, Responses and Chat Completions, preserved through fallback; Max maps through each provider's adapter, and the Claude launcher inherits fixed effort |
| Google account login | Gemini API browser OAuth with PKCE, private token storage, automatic refresh and disconnect; requires your own Google Cloud project and Desktop OAuth client |
| Additional provider cards | Atria and Inception API-key configuration, live model discovery and Chat adapters; their 256k/260k models meet the 256k routing minimum, subject to paid permission, request fit and account capacity |
| Claude launcher | Automatic proxy startup, inherited credential-environment filtering, normal permission prompts, and inherited hooks/MCP disabled |
| Remote messaging | Disabled by default; explicit sender/channel checks and restricted managed Claude tools |
| Dependencies | Locked installation, pinned build tools and security minimums for packages flagged by the dated audit |

The proxy and provider-adapter foundation comes from upstream. The RNB additions and policy changes are documented in [HARDENING.md](HARDENING.md). The `Secured_by_RNB` suffix identifies this fork; it is not a security certification or a promise of unlimited free usage.

For the free provider list, setup and health colors, see the **[Free provider guide](docs/FREE_PROVIDER_GUIDE.md)**. Green requires a completed inference with output within 15 minutes; catalog presence alone stays unverified. Failed routes are skipped during cooldown, and expired successes turn amber. The live guide is under **Routing controls → Verified free failovers**.

Preferred model names are not an allowlist. If they are unavailable or unsuitable,
automatic routing can use any other eligible catalog model in the enabled billing
categories. Every primary and fallback must have **at least 256,000 context
tokens**; smaller and unknown limits are excluded. Within each billing and health tier, models above 512,000 are preferred.

## Maximum reasoning

In **Admin → Reasoning**, set the root policy to **Max**, with Fable, Opus, Sonnet and Haiku set to **Max** or **Inherit**. The root setting controls automatic routing and overrides lower or disabled effort sent by a client. Chat Completions uses the same provider reasoning encoders as the other interfaces, including after provider fallback. The Claude launcher passes a fixed root effort through `CLAUDE_CODE_EFFORT_LEVEL`; restart the CLI to update its session display. The gateway applies a saved change to subsequent requests.

Max requests the strongest reasoning setting supported by the adapter: for example, Inception receives `high`, while OpenRouter receives `reasoning.effort=max`. Providers with no supported effort control keep their own defaults. This does not guarantee a particular internal reasoning length, enable paid access, increase context limits, or remove output/quota limits. Higher effort can use more output tokens and take longer. See the [routing guide](FREE_ROUTING.md#reasoning-policy) for details.

## Admin interface

Manage provider connections, credentials and routing from the local Admin page. The [illustrated Admin guide](docs/ADMIN_GUIDE.md) includes all three supplied screenshots and explains the setup flow.

![FCC Hardened Admin showing connected-account options and cloud provider configuration](docs/images/admin-providers-overview.png)

*Screenshot supplied on 2026-09-20, before the latest routing controls. The displayed OpenRouter count is a catalog count, not available 512k+ capacity. Use **Routing controls** for current policy, route identity and cooldowns.*

## Verified scope

The dated checks in [VALIDATION.md](VALIDATION.md) distinguish the earlier OpenRouter-only release from the current provider-pool update. Earlier checks on **2026-09-20** covered browser login/reset/restart and a live Claude scratch-file coding task; they are not proof of live inference on every newly supported provider. The dependency audit reported no known findings in 108 checked third-party package entries; the local fork itself was not covered by that advisory lookup. See [VALIDATION.md](VALIDATION.md) for commands, versions and exclusions.

These are bounded checks. They do not establish zero vulnerabilities, indefinite task completion or compatibility with every coding harness.

## Start on Windows

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and your preferred native coding client. Clone this repository, open PowerShell in its directory, and run:

```powershell
.\Setup-Hardened.ps1
.\Run-Hardened.ps1
```

Setup uses Python 3.14.0, pinned build tooling and `uv.lock`. It does not install native coding clients or execute upstream download-and-run installers.

Open <http://127.0.0.1:8082/admin>. Username: **admin**. First launch prints a unique temporary password. Change it at first sign-in. If the server started in the background, retrieve the temporary password locally:

```powershell
.\Run-Hardened.ps1 show-initial-password
```

Add your provider API keys in **Admin → Providers**, save them, then open **Routing controls** at <http://127.0.0.1:8082/admin/free>. For account-dependent free tiers, confirm that each saved key belongs to a free account with paid billing disabled. The acknowledgment binds to that key and must be repeated when it changes. No model-selection step is needed. Provider credentials are encrypted for your Windows user, outside the repository. This repository contains no working provider credentials.

Both paid switches start **off**. To permit paid fallback, enable **Allow connected subscriptions** and/or **Allow paid API routes**, choose the billing order, arrange providers with Up/Down, and save. Uncheck providers to exclude them. The default order is free → subscriptions → paid APIs. Paid routes can consume allowance, purchased credits or incur charges; configure provider spending limits because this gateway has no monetary budget cap.

Every selected model and fallback must have **at least 256,000 context tokens**, known tool support and enough estimated room for the request. The status page distinguishes missing credentials, catalog failures, eligible models and cooldowns. **OpenAI / ChatGPT** and **GitHub Copilot** can enter subscription routing after opt-in, subject to those checks. A connected account with a 272k default context now meets the floor; the active default limit is used, not an experimental maximum. See [FREE_ROUTING.md](FREE_ROUTING.md) for provider and interface limits.

The **Actual gateway route** panel shows the last successful provider/model and latest attempt, billing category, context and timestamps. It refreshes every ten seconds while visible; records reset on server restart. These are gateway records across clients. A CLI header or a model's self-description may repeat the fixed client alias and does not establish the actual route.

Gemini API-key discovery uses Google's native, paginated model catalog. Known Google access failures now show a specific explanation in the provider card and routing controls without exposing the upstream key or project ID. `CONSUMER_SUSPENDED` requires resolving the Google Cloud project suspension; retrying the gateway cannot restore access. See [Google's recovery guidance](https://docs.cloud.google.com/resource-manager/docs/project-suspension-guidelines).

## Switch provider or model

**AgentRouter** (`AGENTROUTER_API_KEY`) is available under **Admin → Providers** using its documented [OpenAI-compatible API](https://github.com/agentrouter-org/docs/blob/main/en/qwencode.md) at `https://agentrouter.org/v1`. Save the key in Admin and enable **Allow paid API routes**. Available models enter the same priority controls, manual selection and automatic fallback as other providers, subject to the 256k context floor, tool support and request fit. Promotional credits are not treated as verified free capacity. See the [Admin instructions](docs/ADMIN_GUIDE.md#agentrouter).

**Atria** (`ATRIA_API_KEY`) and **Inception** (`INCEPTION_API_KEY`) appear under Admin → Providers. Save keys there, not in repository files. Both require **Allow paid API routes** because connecting a key or receiving trial credits does not establish zero-cost access. The documented [Atria Dawn Preview](https://api.atria-asi.ai/docs) limit is 256,000 tokens and [Inception Mercury 2.5](https://docs.inceptionlabs.ai/get-started/models) is 260,000. Both meet the 256k floor adopted on 2026-09-21, subject to request fit, tools and account capacity. Inception's public catalog does not verify a saved key.

In **[Routing controls](http://127.0.0.1:8082/admin/free) → Choose provider and model**, choose **Free models**, a provider and an exact model (or the provider's best eligible model), then **Use as first preference**. **Return to automatic selection** removes that preference. Enabled subscription/paid categories can also be selected explicitly.

Manual choice always keeps automatic fallback enabled. If the choice is exhausted, unavailable, excluded or unsuitable for the request, other eligible routes are tried in your saved automatic order before output starts. The manual choice becomes first again when eligible and outside cooldown. All routes retain the 256k floor and existing billing permissions. Settings persist and affect new requests from every client connected to this gateway.

From the repository in PowerShell:

```powershell
.\Run-Hardened.ps1 route list
.\Run-Hardened.ps1 route list --provider open_router
.\Run-Hardened.ps1 route use open_router "<MODEL_ID_FROM_LIST>"
.\Run-Hardened.ps1 route use open_router
.\Run-Hardened.ps1 route status
.\Run-Hardened.ps1 route auto
```

Replace the placeholder with an exact ID returned by `route list`. `use PROVIDER` lets the gateway choose a model within that provider. Both `list` and `use` default to **free**. For already-enabled paid access, use `--billing paid_api` or `--billing subscription`; these flags cannot enable billing or override eligibility. `list --billing all` shows all admitted categories. CLI credentials are loaded privately, never supplied as arguments. The selected preference and actual last successful route are separate fields in CLI status and the web console.

## Claude Code

From any project, run the launcher by its path:

```powershell
& 'C:\path\to\this-repository\Claude-Free.ps1'
```

Or give it a prompt:

```powershell
.\Claude-Free.ps1 -p 'Explain this project'
```

The launcher starts the local proxy if necessary, strips inherited secret environment variables, and keeps normal permission prompts enabled. It disables inherited hooks and MCP servers. A Claude Pro/Max subscription is not needed for this API-provider connection; this does not grant subscription features or access to official Claude models.

To configure the ordinary `claude` command too:

```powershell
.\.venv\Scripts\python.exe .\configure_claude.py
```

This updates user Claude settings and writes a separate `fcc-hardened.settings.json` profile. Its helper reads the local proxy token; provider keys are not copied into Claude settings. Previous settings are backed up with Windows DPAPI under `.fcc-hardened/backups`. Exit old Claude sessions after configuring. An old `Login expired` or subscription prompt may belong to the previous account-login session. `/login` is not the setup path for this proxy.

## Forgotten password

From the same Windows user account:

```powershell
.\Run-Hardened.ps1 reset-password
```

Enter a new password twice at hidden prompts. Reset invalidates browser sessions and preserves provider keys. There is one local administrator per OS-user installation, no shared universal password and no email-reset service. Admin passwords, local proxy tokens and provider API keys are separate credentials.

## Other harnesses and long tasks

Authenticated interfaces: `/v1/messages`, `/v1/responses`, `/v1/chat/completions`. Connect compatible clients with the local proxy credential. A Codex launcher is supplied as `Run-Hardened.ps1 codex`; it was not live-client validated for this release.

Automatic routing ignores client-specified models and manual fallback lists while `AUTO_FREE_MODELS=true`. A saved manual choice is first when eligible; remaining routes follow your billing order. Within each category, recently verified models come before untested/stale models and previously failed models due for recheck. Within each health tier, models above 512k precede the 256k–512k fallback tier; within each context tier, the free preference is **DeepSeek V4.1 Flash → Kimi K3 → Qwen 3.8 Max → GLM 5.3 Flash**, across providers, then other eligible free models. Edit this order in Routing controls. Preferences never establish free pricing or bypass the 256k/tool checks. Each request considers at most 12 candidates, reserving room for other providers and later eligible categories. After failure, another provider in the category being considered is preferred before siblings. This is a bounded availability policy, not a benchmarked best-model selector. Responses requires streaming. Connected subscriptions support Messages/Responses; Chat Completions uses API-key/local routes.

The stable client model ID remains `open_router/openrouter/free` for compatibility; paid opt-ins can route this alias to paid models. Inference targets an explicit discovered model. Opaque upstream routers are excluded because they could choose a smaller context. Caller routing/plugin extras are removed. OpenRouter **free** routes retain zero price ceilings even when paid routes are enabled. Output is capped at 8,192 tokens or the model's smaller limit; context size is not an output allowance.

Free providers can exhaust quotas or go offline. Claude is configured for a 256,000-token compaction window with a 60% trigger. Keep native session persistence enabled and resume saved sessions after an outage. Restart existing Claude sessions through the FCC launcher to pick up the smaller compaction window; compact an already oversized session before resuming. Other harnesses need their own compaction/checkpoint handling. Neither model switching nor a proxy can make every context window or free quota unlimited.

Compaction requests use the same automatic routing. Capacity checks distinguish readable history from encoded reasoning signatures and screenshot bytes, preserving the original payload. Requests estimated to exceed every compatible enabled model receive a context-size error without a quota wait. See [request-fit details](FREE_ROUTING.md#selection-and-failures).

### Continuing a session after tool use

Empty inline system entries in client history are ignored during Chat conversion, while nonempty instructions and tool results are preserved. This fixes the former `requires an inline Anthropic system message to contain text` error. After updating and restarting the gateway, continue the existing Claude session; clearing its history is not required for this issue. Unsupported content still returns a request error, and local conversion failures do not put healthy providers into cooldown.

### OpenRouter daily quota (HTTP 429)

`free-models-per-day` means the OpenRouter account's daily free-request allowance is exhausted. It is different from a full context window or an expired Claude login. The allowance is shared across OpenRouter free models; choosing another one does not restore it. A coding task can require many model requests, including follow-up turns after tool calls.

The gateway recognizes the daily limit and cools OpenRouter free routes until the supplied reset, while allowing funded paid routes when explicitly enabled. It tries the next eligible candidate before output starts. A temporary model 5xx cools that model and permits a sibling attempt; account-wide rate/authentication/balance limits receive broader cooldowns. Once output has started, the request is not replayed through another provider. If all candidates fail, it returns an error with no quota reset or automatic task queue. A client can have its own retry policy.

If no independent free provider remains available, wait for capacity and resume your saved Claude session from the same project:

```powershell
.\Claude-Free.ps1 --resume
```

Daily allowances depend on [OpenRouter's current policy](https://openrouter.ai/docs/api/reference/limits). Purchasing credits is never performed by this app. Paid fallback requires an explicit switch and usable account capacity. Neither free nor paid routing guarantees uninterrupted long tasks.

## Verification and updates

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/core -n 0 -q
.\.venv\Scripts\python.exe security_tests/browser_smoke.py
```

Browser tests use installed Chrome, an isolated profile/home and a synthetic local provider. Read [VALIDATION.md](VALIDATION.md) for dated results and exclusions. Review dependencies and repeat the checks before upgrading. Legacy upstream installer/uninstaller scripts are disabled; upstream release workflows are not enabled for this fork.

## Origin and license

Based on [alishahryar1/free-claude-code](https://github.com/alishahryar1/free-claude-code), upstream commit `2d82b649d681cbdc8deda06d603e0385525fd2fc` (version 6.2.46). The original MIT copyright attribution to Ali Khokhar is retained in [LICENSE](LICENSE).

RNB fork package version: `6.2.46+rnb.1`. The upstream foundation and the RNB modifications retain their respective attribution under the included MIT license.
