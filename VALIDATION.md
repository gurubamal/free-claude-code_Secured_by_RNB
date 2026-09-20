# Validation — 2026-09-20

Release: `6.2.46+rnb.1`, Windows, Python 3.14.0.

## Google account OAuth update — 2026-09-20

Added browser authorization for the public Gemini Developer API using the operator's own Google Desktop OAuth client and project. No Gemini CLI credentials or shared Google client secrets are imported. The existing paid-API switch gates automatic use; reported input context must still meet 512,000 tokens and tool metadata must be available. See [setup and scope](docs/GOOGLE_ACCOUNT.md).

**239 scoped tests passed in 19.95 seconds**:

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/providers/test_gemini.py tests/providers/test_credential_validation.py tests/application/test_connected_accounts.py tests/contracts/test_admin_provider_manifest.py tests/contracts/test_provider_catalog_order.py tests/config/test_admin_status.py tests/config/test_admin_credential_changes.py tests/config/test_provider_catalog.py -n 0 -q --tb=short --show-capture=no
```

The Google tests exercise a real loopback callback with synthetic grants: state/host/duplicate-parameter rejection, PKCE, replay rejection, immediate cancellation and listener cleanup, private persistence, concurrent refresh, revoked-versus-transient refresh failures, disconnect/revocation failure, and project changes. Mock HTTP through the real OpenAI SDK verifies renewable bearer and quota-project headers. Chat ingress tests verify OAuth headers and fallback without forwarding them to another provider. Routing tests keep missing/small context excluded and distinguish paid API from subscription permission.

Installed-Chrome testing passed **nine checks**, including Google setup fields, masked secret entry, the Google authorization URL, cancellation and masked re-editing. No real Google account or live provider was used. The [browser report](security-validation/browser-smoke.json) records this scope. A cancellation-before-task-start race and visible secret-input text were caught and corrected during these checks.

A broader run produced 340 passes, 32 failures and one skip. All 32 failures were reproduced in an isolated checkout of the unchanged `81e1019` commit, in legacy configuration/migration tests that expect upstream defaults or plaintext storage. Those failures remain; this is not a claim that the entire upstream suite passes. Existing Cline wording/catalog-order and unsupported-probe-count expectations were updated to match the fork's already-added providers and this OAuth provider. Ruff checks passed. Dependencies were unchanged.

After restarting the actual local server, at **10:15:24 UTC (15:45:24 IST)** health returned HTTP 200, the routing floor remained 512,000 and `gemini_oauth` reported `CONNECT_ACCOUNT` under `paid_api`. No Google client ID, client secret or project was saved in that installation. Completing real Google consent, account catalog discovery and live Gemini inference therefore remain unvalidated until the operator configures and signs in.

## Earlier release checks

| Check | Observed result |
| --- | --- |
| `pytest security_tests tests/core -n 0 -q` | **844 passed**: 48 targeted security/routing cases and 796 upstream core cases, after dependency upgrades |
| Ruff check of changed/new Python files | Passed |
| Production server + installed Chrome 152.0.7977.83, isolated home/profile | Six smoke checks passed; see [browser-smoke.json](security-validation/browser-smoke.json) |
| Initial password, forced change, subsequent change, sign-out | Passed in Chrome |
| Local password reset | Running browser session revoked; provider configuration preserved; new login succeeded |
| Configuration restart onto a different loopback port | Readiness and cross-port reconnection checks passed; credential persisted; sign-in succeeded |
| Synthetic provider | Authenticated Anthropic request translated to OpenAI Chat and returned a synthetic response |
| Actual Claude Code 2.1.269, nonsensitive prompt | Exit 0; `CLAUDE_PROXY_OK`, using the local proxy |
| Ordinary user Claude configuration | Exit 0; `GLOBAL_CONFIG_OK` |
| Native Claude coding task in a temporary directory | Exit 0; created `smoke.txt` with the exact requested `FCC_WORKING` content |
| PowerShell launcher | Exit 0; `POWERSHELL_LAUNCH_OK` |
| OpenAI Chat endpoint, actual free router | HTTP 200; `CHAT_ROUTE_OK`; response identified a free model |
| `pip-audit 2.10.1` against installed packages | **0 known findings in 108 checked third-party package entries**. The local fork itself was skipped because its version is not published on PyPI. See [dependency-audit.json](security-validation/dependency-audit.json). |

The initial upstream lock audit flagged six packages. Updated: click 8.5.0, cryptography 50.0.1, pygments 2.21.0, python-multipart 0.0.32, starlette 1.6.0 and urllib3 2.8.0. Security minimums are recorded in `pyproject.toml`; resolved versions and hashes are in `uv.lock`. The successful test run above used these updated versions.

Browser and unit checks used synthetic credentials and isolated storage. Actual provider/Claude smoke tests used bounded, nonsensitive prompts and an isolated scratch coding directory. Provider keys and authenticated account responses are not included in this repository. An initial very short-output live probe returned HTTP 200 with whitespace; it was not counted as a successful content test. Subsequent checks verified exact response text and actual file creation.

## Daily-quota handling follow-up — 2026-09-20

An actual OpenRouter daily free-request exhaustion exposed misleading short-retry messaging and repeated attempts. The fix recognizes the explicit daily-limit discriminator (or the legacy daily-limit message), displays the reported UTC reset, and treats this condition as terminal for the current request. It does not invent a reset when metadata is missing or malformed, enable paid routing, queue tasks, or increase the account allowance.

After the fix, **1,103 tests passed** with this scope:

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/core tests/providers/test_failure_policy.py tests/providers/test_execution_failure_boundary.py tests/providers/test_open_router.py tests/providers/test_provider_admission.py tests/providers/test_streaming_errors.py tests/providers/test_openai_compat_5xx_retry.py tests/providers/test_nvidia_nim_degraded_retry.py -n 0 -q --tb=short
```

The new regression checks include the actual Messages provider execution path and both streaming/nonstreaming Chat requests. They assert one upstream attempt for daily exhaustion, retained bounded retries for generic temporary 429s, reset parsing, malformed/missing metadata, and omission of upstream retry-history prose. Two accepted-stream tests first reproduced five unwanted provider calls; both pass with exactly one call after the transport correction. Ruff and Git whitespace checks passed for this change.

The local proxy was restarted. One bounded live `/v1/messages` failure check returned HTTP 429 in **0.48 seconds** with the new daily-quota message, provider-reported UTC reset, and `x-should-retry: false`. This verifies the failure path; it is not a successful inference or proof that quota has recovered. The earlier browser and dependency checks were not rerun for this follow-up. Account responses and credentials are not stored in this repository.

These results do not establish zero vulnerabilities or indefinite task completion. The entire upstream test suite was not run: its legacy default-authentication/config-migration expectations differ from this fork. Native Codex, all upstream providers, all operating systems, every OAuth flow, and prolonged quota-exhaustion/compaction scenarios were not live tested. The dependency audit is a dated known-advisory check, not a source-code security certification. A separate independent security review remains appropriate before broader deployment.

## Automatic provider pool and 512k minimum — 2026-09-20

This follow-up replaces the OpenRouter-only automatic route with a shared provider pool across Messages, Responses and Chat. Every candidate requires free admission, tool support, at least **512,000 context tokens** and a request-size fit. It does not relax this floor when capacity runs out. The earlier live successes above used the prior route; they are not evidence of successful live inference through all new integrations.

The expanded scoped run passed **1,209 tests** (57.49 seconds):

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/core tests/application/test_execution.py tests/application/test_routing.py tests/providers/test_failure_policy.py tests/providers/test_execution_failure_boundary.py tests/providers/test_open_router.py tests/providers/test_provider_admission.py tests/providers/test_streaming_errors.py tests/providers/test_openai_compat_5xx_retry.py tests/providers/test_nvidia_nim_degraded_retry.py -n 0 -q --tb=short
```

Coverage includes exact 512,000 admission versus 511,999 rejection; smaller/unknown context exclusions; provider-specific context limits; paid/surcharge rejection; opaque-router exclusions; API ingress stripping of paid extras; authenticated Messages/Responses/Chat cross-provider fallback with synthetic providers; pre-output timeout fallback; no replay after committed output; credential-bound acknowledgments and revocation; persistent credential-scoped cooldowns; retry hints; read-only quota exhaustion/recovery; model-specific access errors; foreign pagination rejection; and local context settings bounded by the model's capacity. Legacy manual-routing tests now explicitly disable automatic mode so their original contract is exercised. The exception contract was extended to carry an optional immutable retry delay.

The installed-Chrome production-server smoke passed **seven checks**. It covered existing login/change/reset/restart behavior and the new free-routing page: context display, cooldown display, refresh and credential-acknowledgment request. The new page's API responses were synthetic browser fixtures; no real provider keys were entered. The backend acknowledgment and API authentication are separately covered by unit tests. See [browser-smoke.json](security-validation/browser-smoke.json) and the [synthetic status screenshot](security-validation/free-routing.png).

At **2026-09-20 07:50 UTC**, live catalog discovery found **five eligible OpenRouter models** under the explicit zero-price/tool/512k rules. Only OpenRouter had a hosted key in the managed configuration. A bounded nonsensitive Messages probe returned HTTP 403 in 0.39 seconds; this was a failure-path check, not successful inference. A separate read-only OpenRouter key check returned HTTP 200 and reported 0 of 50 daily free requests remaining. This prompted the read-only quota preflight, which avoids generation attempts when the account reports zero capacity. No account response or key was saved in this repository.

Independent-provider inference remains tested with synthetic providers only: the other hosted credentials were not configured. No successful live coding task is claimed for this pool update, and no 512k-sized prompt or prolonged multi-provider task was run. Browser page behavior and catalog capability claims do not establish provider availability, billing state or long-context accuracy. The earlier dependency audit remains dated; dependencies were not changed for this update.

After adding recognition of Mistral's primary `max_context_length` and `function_calling` fields, the complete free-pool test file passed **33 tests**. This targeted rerun includes the newly added primary-versus-secondary metadata case. Claude's configured compaction window was aligned to **512,000**, retaining the 60% trigger. Both persisted Claude settings files were checked: the expected values were present and the provider key was absent. No successful long-context generation is inferred from setting these values.

At **2026-09-20 07:55 UTC**, after restart, authenticated live status returned HTTP 200, a 512,000-token floor, five catalog-eligible models and zero outside cooldown. OpenRouter's reason was `daily_quota_exhausted` from the new read-only preflight. The displayed five-minute retry check does not claim that quota will reset then. No additional inference request was needed for this verification.

The final targeted security + free-pool rerun passed **78 tests** after the Claude-window alignment. Ruff passed on every changed/new Python file, Git whitespace checks passed, and staged files passed an in-memory scan for the managed provider/proxy credentials and common token patterns. These scans are bounded checks, not proof of an absence of all secrets or vulnerabilities.

## Connected-account model discovery fix — 2026-09-20

The automatic-pool update incorrectly rejected OpenAI provider construction before a read-only catalog request. The local daemon recorded `Provider is outside the automatic free policy` at 13:37:54 IST, while Admin showed a generic settings error. Providers outside the automatic free policy now receive a discovery-only wrapper: catalog listing and cleanup delegate to the provider, while both generation methods reject the request. Manual mode retains its existing provider behavior. OpenAI has not been added to the automatic free pool.

The following scoped regression run passed **149 tests** in 47.08 seconds:

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/providers/test_openai_codex_provider.py tests/config/test_admin_status.py -n 0 -q --tb=short
```

New cases verify that OpenAI catalog calls reach the underlying provider, generation calls do not, cleanup delegates, manual mode is unchanged, and Admin distinguishes free-policy support. The production-server smoke in installed Chrome passed **eight checks**, including a connected OpenAI card with a synthetic catalog count and a separate free-policy exclusion. See [browser-smoke.json](security-validation/browser-smoke.json) and [the synthetic OpenAI card](security-validation/openai-discovery.png). These browser fixtures do not authenticate to OpenAI. Ruff and Git whitespace checks passed; dependencies did not change.

After restarting the local daemon, a read-only check at **2026-09-20 08:18:51 UTC** found no saved ChatGPT credentials and reported `disconnected`. A live OpenAI catalog request could therefore not be verified in this check. Authenticated free-routing status returned HTTP 200 at 08:18:55 UTC: automatic routing enabled, a 512,000-token floor, five catalog-eligible models, and zero available outside cooldown; OpenRouter still reported daily quota exhaustion. No inference was attempted for this fix. A reconnect and successful live catalog response remain necessary to validate this user's OpenAI connection.

## Optional paid fallback, route visibility and documentation — 2026-09-20

This update adds separately disabled-by-default subscription and paid-API switches, category/provider priorities, exclusions and actual route identity in Admin. The minimum remains 512,000 for every automatic route. It adds Command Code and Kimchi Chat adapters and admits existing DeepSeek/Cline API adapters into paid automatic routing. Command Code Anthropic-only catalog entries are excluded. Connected subscriptions support Messages/Responses; they are excluded from Chat Completions.

The final broad scoped run passed **1,233 tests in 102.50 seconds**:

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/core tests/application/test_execution.py tests/application/test_routing.py tests/providers/test_openai_codex_provider.py tests/providers/test_open_router.py tests/providers/test_deepseek.py tests/providers/test_cline_pass.py tests/providers/test_provider_runtime.py tests/providers/test_provider_admission.py tests/config/test_admin_status.py -n 0 -q --tb=short --show-capture=no
```

An earlier run exposed 18 legacy provider-constructor tests whose unconstrained mock settings implicitly enabled automatic mode. Their shared fixture now explicitly selects manual mode; automatic policy remains tested separately. The final constructor checks include both new adapters. A later Cline exact-ID registry fallback correction passed all **15 paid-routing tests**. The broad run and targeted follow-up are separate scopes, not two independent full-suite runs.

Regression checks cover default paid denial, explicit opt-ins, strict context admission, category priority before sibling models, reserved fallback-category slots, exclusions, free quotas separated from paid balance, retained zero-price guards, Admin authentication/CSRF/validation and model-specific 503 handling. Real DeepSeek adapters with synthetic SDK responses verify that Messages and Responses actually invoke the second model after the first returns 503. This catches a transport recovery circuit that previously returned the first error again without calling the sibling. Automatic mode now delegates cooldown ownership to the router while preserving rate/concurrency admission; manual coordinated recovery is unchanged.

Installed-Chrome production-server testing passed **eight checks**. The route panel showed synthetic provider/model, billing, context and timestamps, and updated a changed attempt status on the ten-second timer. Paid switches, priority, exclusions and saving were exercised alongside login/reset/restart checks. The [browser report](security-validation/browser-smoke.json) and [routing screenshot](security-validation/free-routing.png) contain synthetic state. The [three supplied documentation images](docs/ADMIN_GUIDE.md) were copied byte-for-byte from the maintainer's screenshots, visually reviewed and checked for embedded PNG text/EXIF; they show historical provider UI and are not inference evidence.

### Bounded live observations

- **08:27:39 UTC:** OpenAI catalog discovery returned five models after using a numeric client version instead of the package's `+rnb.1` suffix. All five reported 272,000 default context tokens. Some experimental maxima were larger, but none qualified under the retained 512k default-context rule. Catalog discovery is not subscription inference validation.
- **08:54:29 UTC:** a DeepSeek request failed with 503. Detailed logs showed one physical POST followed by a sibling error from the transport circuit. This exposed the second recovery-layer defect; two logged route attempts were not two provider calls.
- **08:58:25 UTC:** gateway status recorded a later successful `deepseek/deepseek-flash` request from the connected client. The client's MiniMax label did not identify the gateway's route.
- **09:06:47 UTC:** after restarting with the transport and visibility fixes, one bounded Messages probe returned HTTP 200 and exact `ROUTING_OK` in **3.70 seconds**, using `deepseek/deepseek-flash`, category `paid_api`, catalog context 1,000,000. Authenticated status exposed the matching successful route and completion timestamp. This is one small live response, not a live forced-fallback or long-context benchmark.

The live installation explicitly enabled subscriptions and paid APIs with free → subscription → paid API priority; shipped defaults remain free-only. OpenRouter access was constrained by daily free exhaustion and absent paid balance. Cline, Command Code and Kimchi had no saved credentials during these checks, so their inference was not live validated. Anonymous Cline catalog inspection found no exact matches in its dedicated registry, prompting exact-ID fallback to the general OpenRouter registry; primary provider limits retain precedence. Missing metadata still excludes a model.

Credentials, authenticated account payloads and private login records are excluded from public artifacts. Before publication, all 875 tracked/new files were scanned in memory against ten current private credential/identity/hash values and common token patterns; no matches or private credential paths were found. The staged Git index is checked again before committing. These bounded scans do not prove an absence of every possible secret or vulnerability. The dependency set did not change and the earlier advisory audit was not rerun. No prolonged task, 512k prompt, every provider account or all operating systems were validated.

## Empty inline system entries during session continuation — 2026-09-20

An inline `{"role":"system","content":[]}` in a resumed history triggered a deterministic Chat conversion exception. Automatic execution also incorrectly remapped that local request error to HTTP 503 and cooled the selected model. Retrying the same history therefore repeated the failure rather than testing provider availability.

The converter now omits empty inline system entries without inserting an extra user turn. Nonempty instructions, tool calls/results and original input objects are preserved. Unsupported non-text system blocks remain errors. Automatic execution preserves the original application error kind/status, and local invalid-request failures update attempt status without creating an upstream cooldown.

Three converter cases failed before the fix. API regression cases also reproduced HTTP 503 for streaming and non-streaming continuation. After the fix, **149 targeted tests passed**, followed by **1,153 scoped tests in 68.51 seconds**:

```powershell
.\.venv\Scripts\python.exe -m pytest security_tests tests/core tests/providers/test_converter.py tests/providers/test_deepseek.py tests/providers/test_open_router.py tests/application/test_execution.py -n 0 -q --tb=short --show-capture=no
```

The new API tests use a real DeepSeek adapter with synthetic SDK responses. They verify that continued history reaches generation, instructions/tool results survive, unsupported content returns HTTP 400 without making an upstream call, and a subsequent valid request can use the same provider immediately. Ruff and whitespace checks passed. Browser assets and dependencies did not change, so their previous dated checks were not rerun.

After restarting the local gateway, a bounded live Messages request at **09:22:40 UTC (14:52:40 IST)** included two empty inline system entries and synthetic tool history. It returned HTTP 200 with exact `RESUME_OK` in **3.61 seconds** through `deepseek/deepseek-flash`. No real session content or project files were sent in this probe. This confirms the reported conversion shape works after restart; it does not guarantee uninterrupted future provider capacity. The existing client session can continue without clearing its history for this issue.
