# free-claude-code_Secured_by_RNB

A local gateway for coding assistants, maintained as the **RNB hardening fork**. It connects supported clients to automatic routing across configured free providers and adds recoverable administrator access and protected credential storage on Windows.

This repository builds on an existing MIT-licensed project; the upstream source and license are credited below. This guide describes the RNB version and its changes.

## RNB changes

| Area | Implemented in this fork |
| --- | --- |
| Local access | Loopback-only binding and mandatory proxy authentication |
| Administrator login | Unique temporary password, required first-login change, expiring browser sessions and local recovery |
| Credential storage | Windows user-bound DPAPI encryption for managed provider credentials and Admin state; permanent Admin passwords are hashed |
| Password reset | Invalidates browser sessions while preserving provider configuration |
| Automatic routing | Live free-model discovery, 512k+ context floor, independent-provider fallback, persistent cooldowns and paid-routing guards |
| Claude launcher | Automatic proxy startup, inherited credential-environment filtering, normal permission prompts, and inherited hooks/MCP disabled |
| Remote messaging | Disabled by default; explicit sender/channel checks and restricted managed Claude tools |
| Dependencies | Locked installation, pinned build tools and security minimums for packages flagged by the dated audit |

The proxy and provider-adapter foundation comes from upstream. The RNB additions and policy changes are documented in [HARDENING.md](HARDENING.md). The `Secured_by_RNB` suffix identifies this fork; it is not a security certification or a promise of unlimited free usage.

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

Add your provider API keys in **Admin → Providers**, save them, then open **Automatic free routing** at <http://127.0.0.1:8082/admin/free>. For account-dependent free tiers, confirm that each saved key belongs to a free account with paid billing disabled. The acknowledgment binds to that key and must be repeated when it changes. No model-selection step is needed. Provider credentials are encrypted for your Windows user, outside the repository. This repository contains no working provider credentials.

The status page distinguishes missing keys, missing acknowledgments, catalog failures, eligible models and cooldowns. It displays each eligible model’s context limit. Every selected model and fallback must have **at least 512,000 context tokens**, known tool support, eligible free access and enough estimated room for the actual request. Unknown or smaller context windows are excluded. See [FREE_ROUTING.md](FREE_ROUTING.md) for the provider policies and limits.

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

Automatic routing ignores client-specified models and manual fallback lists while free mode is active. It discovers eligible models from configured providers, tries independent providers before additional models from the same provider, and prefers the last successful eligible model on later requests. It uses current catalog/account checks, not a benchmarked best-model selector. All three API interfaces use this pool; Responses currently requires streaming.

The stable client model ID remains `open_router/openrouter/free` for compatibility. It is a gateway alias: inference is sent to a selected explicit model, potentially at another provider. Opaque upstream auto routers are excluded because they could choose a model below the 512k floor. Paid routing/plugin extras are removed; OpenRouter requests additionally carry zero price ceilings. Output is capped at 8,192 tokens or the selected model's smaller output limit. The 512k minimum is a context-window requirement, not an output-token allowance.

Free providers can exhaust quotas or go offline. Claude is configured for a 512,000-token compaction window with a 60% trigger. Keep native session persistence enabled and resume saved sessions after an outage. Other harnesses need their own compaction/checkpoint handling. Neither model switching nor a proxy can make every context window or free quota unlimited.

### OpenRouter daily quota (HTTP 429)

`free-models-per-day` means the OpenRouter account's daily free-request allowance is exhausted. It is different from a full context window or an expired Claude login. The allowance is shared across OpenRouter free models; choosing another one does not restore it. A coding task can require many model requests, including follow-up turns after tool calls.

The gateway recognizes this daily limit, persists an OpenRouter-wide cooldown until the supplied reset, and tries an eligible independent provider if no output has been delivered. Temporary failures also cool down instead of causing repeated same-provider retries. Each request considers at most 12 candidates. Once output has started, the request is not replayed through another provider. If all eligible routes are unavailable, the gateway returns an error; it never resets an account allowance, enables paid fallback, or queues a task for automatic restart. A client can have its own retry policy.

If no independent free provider remains available, wait for capacity and resume your saved Claude session from the same project:

```powershell
.\Claude-Free.ps1 --resume
```

Daily allowances depend on OpenRouter's current account policy; see [OpenRouter limits](https://openrouter.ai/docs/api/reference/limits). Purchasing credits can raise the free-model request ceiling, but it is a paid account action and is never performed by this app. No paid model fallback is enabled. Hosted free-only routing does not guarantee uninterrupted long tasks.

## Verification and updates

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/core -n 0 -q
.\.venv\Scripts\python.exe security_tests/browser_smoke.py
```

Browser tests use installed Chrome, an isolated profile/home and a synthetic local provider. Read [VALIDATION.md](VALIDATION.md) for dated results and exclusions. Review dependencies and repeat the checks before upgrading. Legacy upstream installer/uninstaller scripts are disabled; upstream release workflows are not enabled for this fork.

## Origin and license

Based on [alishahryar1/free-claude-code](https://github.com/alishahryar1/free-claude-code), upstream commit `2d82b649d681cbdc8deda06d603e0385525fd2fc` (version 6.2.46). The original MIT copyright attribution to Ali Khokhar is retained in [LICENSE](LICENSE).

RNB fork package version: `6.2.46+rnb.1`. The upstream foundation and the RNB modifications retain their respective attribution under the included MIT license.
