# Secured_by_RNB: scope and limitations

This is a modified copy of [alishahryar1/free-claude-code](https://github.com/alishahryar1/free-claude-code), based on commit `2d82b649d681cbdc8deda06d603e0385525fd2fc` (upstream 6.2.46). The original MIT license and attribution are retained. The suffix is a project name, **not a security certification**.

## Changed boundaries

- Bind only to a loopback IP. Proxy authentication is mandatory, with a generated persistent token; `freecc` is not accepted.
- Protect all Admin pages and coding APIs with an independent local administrator login. Default username: `admin`; no universal default password. First launch generates a unique temporary password and requires replacement.
- Hash permanent passwords with salted PBKDF2-HMAC-SHA256 (600,000 iterations). Use HTTP-only, SameSite Strict, eight-hour sessions, a bounded login-attempt throttle, same-origin checks and a required header for mutations. Reset revokes sessions in running processes through a credential revision check.
- On Windows, encrypt managed provider settings, OpenAI refresh credentials and Admin state with current-user DPAPI before writing. The initial temporary password is also DPAPI protected for local retrieval and is deleted when changed/reset. Permanent passwords are stored as hashes only.
- Local recovery uses hidden password input. It replaces only the administrator credential; provider keys remain intact. There is one administrator per OS-user installation, not a public multi-tenant account system or email-reset service.
- Use a separate `.fcc-hardened` directory and do not silently import another FCC installation's dotenv files. The supported launcher strips inherited broker/cloud/provider environment credentials before launching clients.
- Disable messaging by default. Telegram requires a sender ID; Discord requires explicit channel IDs. Remote managed Claude requests use restricted read-only tools and disable inherited hooks/MCP configuration. Native-client permissions are still important: this is not an OS sandbox for every client.
- Browser coding defaults to workspace permissions and approval requests. The inherited unrestricted mode is rejected. The local Claude wrapper forces normal permissions and disables inherited hooks and MCP servers.
- Require HTTPS for remote provider endpoints, with loopback HTTP allowed for local models.
- Pin the build tools and use the checked-in dependency lock. No native client installer or mutable upstream shell installer is invoked by the supported setup script.

## Free automatic model routing

Default `AUTO_FREE_MODELS=true` sends Anthropic Messages and OpenAI Responses requests through the shared automatic pool. Chat Completions always uses the free pool. The advertised single client alias is retained for compatibility, but the upstream target is an explicit discovered model. Every target and fallback requires known tool support, a context window of at least **512,000 tokens**, and enough estimated space for the request. Unknown/smaller context windows and opaque auto routers are excluded. Catalogs refresh after five minutes or a key/acknowledgment change; failed discovery removes stale eligibility.

See [FREE_ROUTING.md](FREE_ROUTING.md) for the 10 hosted-provider and three local-server policies. Some services publish per-model prices; other free tiers depend on the account's billing configuration. Account-dependent providers require a credential-bound acknowledgment that paid billing is disabled. This acknowledgment is not independent verification: enabling paid billing later can invalidate it, and the user must revoke it. Provider pricing can also change between catalog checks. OpenRouter requests additionally enforce zero price ceilings. No other provider is claimed to support that request-level price guard.

Caller model/fallback/plugin routing extras are removed in automatic mode. Cloud destinations are fixed to reviewed provider endpoints; discovery does not forward credentials to pagination on a different host/path or follow redirects. Local fallback requires loopback. Request outputs are capped at 8,192 tokens or a smaller model output limit. Models are tried across independent providers first, with at most 12 candidates per request and one provider admission attempt per candidate. Failure cooldowns are shared between API interfaces and persisted with protected local storage. Retry-After is honored when exposed; OpenRouter's explicit daily reset suppresses all sibling models until that reset. No task queue, account-quota reset, or paid rescue route is implemented.

Fallback is allowed only before the gateway emits the first output chunk. A delivered stream or tool call is never silently replayed. Empty, interrupted and error streams do not earn a success preference. The app sends the existing conversation to a fallback; it does not reconstruct a provider's hidden state. Clients should send self-contained history and preserve their sessions. Selection is a bounded policy with a last-success preference, not a measured guarantee of coding quality.

Claude is configured for an explicit 512,000-token auto-compaction window and an earlier 60% trigger. The 60% trigger leaves room within the 512k minimum for output and tool results; it is a client configuration, not a long-duration reliability benchmark. A session may still need resuming after provider quota exhaustion. The proxy does not invent or silently discard conversation history. Other harnesses must implement their own compaction, session persistence and tool-result recovery. Anthropic Messages, OpenAI Responses and OpenAI Chat Completions are supported interfaces; arbitrary proprietary harnesses are not automatically compatible.

## Remaining trust and risks

There is no claim of zero security bugs, unlimited tokens, unlimited availability, or guaranteed task completion. A process running as your Windows user can use your DPAPI keys. Native coding tools can affect files you authorize them to access. Free hosted providers receive the prompts/code sent to them and have their own data policies. The original application and third-party dependencies are substantial; passing targeted tests is not a complete penetration test. POSIX storage uses owner-only file permissions, not the Windows encryption mechanism, and was not validated in this Windows delivery.

The supported entry points are `Setup-Hardened.ps1`, `Run-Hardened.ps1`, `Claude-Free.ps1` and `hardened.py`. Legacy installer/uninstaller scripts are disabled in this fork. Upstream documents describe upstream behavior and may not describe this fork. Upstream release workflows are not authorized for publication from this project.

## Validation

See [VALIDATION.md](VALIDATION.md) for the dated checks and their limits. Tests use synthetic credentials and isolated homes. Browser testing uses installed Chrome and a temporary profile. Real-provider checks use a bounded, nonsensitive smoke prompt and a scratch-directory coding task; no project files or account payloads are included in the public evidence.

Primary references: [Windows DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata), [OpenRouter free routing](https://openrouter.ai/docs/cookbook/get-started/free-models-router-playground), [OpenRouter provider price limits](https://openrouter.ai/docs/guides/routing/provider-selection), [Claude environment variables](https://code.claude.com/docs/en/env-vars).
