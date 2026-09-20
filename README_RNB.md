# free-claude-code_Secured_by_RNB

A Windows-first hardening fork of [Free Claude Code](https://github.com/alishahryar1/free-claude-code), with local Admin login/recovery and automatic free-model routing for coding clients.

**Security hardening is not a guarantee of zero vulnerabilities or unlimited tokens.** Read [HARDENING.md](HARDENING.md) and [VALIDATION.md](VALIDATION.md).

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

## Verification and updates

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/core -n 0 -q
.\.venv\Scripts\python.exe security_tests/browser_smoke.py
```

Browser tests use installed Chrome, an isolated profile/home and a synthetic local provider. Read [VALIDATION.md](VALIDATION.md) for dated results and exclusions. Review dependencies and repeat the checks before upgrading. Legacy upstream installer/uninstaller scripts are disabled; upstream release workflows are not enabled for this fork.

## Attribution

Based on upstream commit `2d82b649d681cbdc8deda06d603e0385525fd2fc` (6.2.46). Fork package version: `6.2.46+rnb.1`. The original MIT license and copyright remain in [LICENSE](LICENSE). The original material below the notice in [README.md](README.md) describes upstream behavior and is not this fork's installation guide.
