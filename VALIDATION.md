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

These results do not establish zero vulnerabilities or indefinite task completion. The entire upstream test suite was not run: its legacy default-authentication/config-migration expectations differ from this fork. Native Codex, all upstream providers, all operating systems, every OAuth flow, and prolonged quota-exhaustion/compaction scenarios were not live tested. The dependency audit is a dated known-advisory check, not a source-code security certification. A separate independent security review remains appropriate before broader deployment.
