# Validation — 2026-09-20

Release: `6.2.46+rnb.1`, Windows, Python 3.14.0.

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
