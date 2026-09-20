# free-claude-code_Secured_by_RNB

A local gateway for coding assistants, maintained as the **RNB hardening fork**. It connects supported clients to automatic free-model routing through OpenRouter and adds recoverable administrator access and protected credential storage on Windows.

This repository builds on an existing MIT-licensed project; the upstream source and license are credited below. This guide describes the RNB version and its changes.

## RNB changes

| Area | Implemented in this fork |
| --- | --- |
| Local access | Loopback-only binding and mandatory proxy authentication |
| Administrator login | Unique temporary password, required first-login change, expiring browser sessions and local recovery |
| Credential storage | Windows user-bound DPAPI encryption for managed provider credentials and Admin state; permanent Admin passwords are hashed |
| Password reset | Invalidates browser sessions while preserving provider configuration |
| Automatic routing | One free-model route, zero provider price ceilings and filtering of caller-paid routing/plugin extras |
| Claude launcher | Automatic proxy startup, inherited credential-environment filtering, normal permission prompts, and inherited hooks/MCP disabled |
| Remote messaging | Disabled by default; explicit sender/channel checks and restricted managed Claude tools |
| Dependencies | Locked installation, pinned build tools and security minimums for packages flagged by the dated audit |

The proxy and provider-adapter foundation comes from upstream. The RNB additions and policy changes are documented in [HARDENING.md](HARDENING.md). The `Secured_by_RNB` suffix identifies this fork; it is not a security certification or a promise of unlimited free usage.

## Verified scope

On **2026-09-20**, the Windows build passed **1,103 scoped tests** after the daily-quota fix. Earlier checks that day covered browser login/reset/restart and a live Claude scratch-file coding task. The dependency audit reported no known findings in 108 checked third-party package entries; the local fork itself was not covered by that advisory lookup. See [VALIDATION.md](VALIDATION.md) for commands, versions and exclusions.

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

Add your own OpenRouter API key in Admin. Automatic routing uses `openrouter/free`; no model-selection step is needed. Provider credentials are encrypted for your Windows user, outside the repository. This repository contains no working provider credentials.

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

This updates user Claude settings and writes a separate `fcc-hardened.settings.json` profile. Its helper reads the local proxy token; the OpenRouter key is not copied into Claude settings. Previous settings are backed up with Windows DPAPI under `.fcc-hardened/backups`. Exit old Claude sessions after configuring. An old `Login expired` or subscription prompt may belong to the previous account-login session. `/login` is not the setup path for this proxy.

## Forgotten password

From the same Windows user account:

```powershell
.\Run-Hardened.ps1 reset-password
```

Enter a new password twice at hidden prompts. Reset invalidates browser sessions and preserves provider keys. There is one local administrator per OS-user installation, no shared universal password and no email-reset service. Admin passwords, local proxy tokens and provider API keys are separate credentials.

## Other harnesses and long tasks

Authenticated interfaces: `/v1/messages`, `/v1/responses`, `/v1/chat/completions`. Connect compatible clients with the local proxy credential. A Codex launcher is supplied as `Run-Hardened.ps1 codex`; it was not live-client validated for this release.

Automatic routing ignores client-specified paid model names while free mode is active. Free requests carry zero provider price ceilings, exclude caller-paid routing/plugin extras, and cap output at 8,192 tokens. OpenRouter selects an available free model with the required features; this is not a benchmarked best-model selector.

Free providers can exhaust quotas or go offline. Claude is configured for earlier automatic context compaction. Keep native session persistence enabled and resume saved sessions after an outage. Other harnesses need their own compaction/checkpoint handling. Neither model switching nor a proxy can make every context window or free quota unlimited.

### OpenRouter daily quota (HTTP 429)

`free-models-per-day` means the OpenRouter account's daily free-request allowance is exhausted. It is different from a full context window or an expired Claude login. The allowance is shared across OpenRouter free models; choosing another one does not restore it. A coding task can require many model requests, including follow-up turns after tool calls.

The gateway recognizes this daily limit, stops its immediate retries and displays the provider-reported reset time in UTC. It also stops retry/recovery when this limit arrives after an Anthropic-compatible stream has started. Generic temporary rate limits still receive bounded retries. Requests are not queued for automatic restart, and a separate client can have its own retry policy.

Wait for the reset, then resume your saved Claude session from the same project:

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
