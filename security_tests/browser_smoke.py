"""Isolated production-server + synthetic provider + installed Chrome smoke.

No real keys, home, Chrome profile, provider or broker are used. Ephemeral login
credentials stay in RAM; screenshots contain only the signed-out login screen.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, payload):
        content = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.reply(
            {
                "object": "list",
                "data": [
                    {"id": "test-model", "object": "model", "owned_by": "synthetic"}
                ],
            }
        )

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body.get("model") == "test-model"
        if body.get("stream"):
            chunk = {
                "id": "test-chat",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "Synthetic response.",
                        },
                        "finish_reason": None,
                    }
                ],
            }
            final = {
                **chunk,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 3,
                    "total_tokens": 7,
                },
            }
            content = (
                "data: "
                + json.dumps(chunk)
                + "\n\ndata: "
                + json.dumps(final)
                + "\n\ndata: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.reply(
                {
                    "id": "test-chat",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Synthetic response.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 3,
                        "total_tokens": 7,
                    },
                }
            )


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    from free_claude_code.harnesses.environment import client_environment

    clean = client_environment(dict(os.environ), proxy_root_url="http://127.0.0.1")
    os.environ.clear()
    os.environ.update(clean)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    browser_env = dict(os.environ)
    import httpx
    from playwright.sync_api import sync_playwright

    checks = []
    with tempfile.TemporaryDirectory(prefix="fcc-hardened-smoke-") as directory:
        os.environ["USERPROFILE"] = directory
        os.environ["HOME"] = directory
        from free_claude_code.config.env_migrations import atomic_write_managed_config
        from free_claude_code.config.loader import ManagedConfigStore
        from free_claude_code.core.admin_accounts import AdminAccounts

        store = ManagedConfigStore()
        store.initialize(env={})
        snapshot = store.read(env={})
        token = snapshot.settings.proxy_auth_token
        port = unused_port()
        mock = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=mock.serve_forever, daemon=True).start()
        values = dict(snapshot.managed)
        values.update(
            {
                "PORT": str(port),
                "FCC_OPEN_BROWSER": "false",
                "AUTO_FREE_MODELS": "false",
                "MODEL": "lmstudio/test-model",
                "LM_STUDIO_BASE_URL": f"http://127.0.0.1:{mock.server_port}/v1",
            }
        )
        atomic_write_managed_config(values)
        lines = queue.Queue()
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "hardened.py"), "serve"],
            cwd=ROOT,
            env=dict(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def capture():
            for line in process.stdout:
                if line.startswith("Temporary Admin password: "):
                    lines.put(line.partition(": ")[2].strip())

        threading.Thread(target=capture, daemon=True).start()
        try:
            temporary = lines.get(timeout=30)
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=20) as http:
                deadline = time.monotonic() + 40
                while True:
                    try:
                        if http.get(url + "/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("Production server did not start")
                    time.sleep(0.2)
                checks.append("production server startup")
                assert http.get(url + "/admin/api/config").status_code == 401
                assert http.head(url + "/v1/messages").status_code == 401
                result = http.post(
                    url + "/v1/messages",
                    headers={"x-api-key": token},
                    json={
                        "model": "fable",
                        "max_tokens": 64,
                        "messages": [
                            {"role": "user", "content": "Synthetic smoke request"}
                        ],
                    },
                )
                assert result.status_code == 200, (
                    f"Synthetic provider request returned {result.status_code}"
                )
                assert any(
                    block.get("text") == "Synthetic response."
                    for block in result.json()["content"]
                )
                checks.append(
                    "authenticated Anthropic-to-OpenAI synthetic provider roundtrip"
                )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    channel="chrome", headless=True, env=browser_env
                )
                context = browser.new_context(viewport={"width": 1366, "height": 900})
                allowed_origins = {url}
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if any(
                            route.request.url.startswith(origin + "/")
                            for origin in allowed_origins
                        )
                        else route.abort()
                    ),
                )
                page = context.new_page()
                failures = []
                page.on("pageerror", lambda error: failures.append(str(error)))
                page.goto(url + "/admin")
                page.wait_for_url("**/admin/login")
                output = ROOT / "security-validation"
                output.mkdir(exist_ok=True)
                page.screenshot(path=str(output / "login.png"), full_page=True)
                page.locator("#password").fill(temporary)
                page.locator("#login button").click()
                page.locator("#change").wait_for(state="visible")
                password = "Synthetic Browser River 427!"
                page.locator("#newPassword").fill(password)
                page.locator("#confirm").fill(password)
                page.locator("#change button").click()
                page.locator("#login").wait_for(state="visible")
                page.locator("#password").fill(password)
                page.locator("#login button").click()
                page.wait_for_url(url + "/admin")
                page.locator("#providerGroups .provider-strip").first.wait_for(
                    timeout=20000
                )
                checks.append(
                    "Chrome first login, mandatory change and full Admin render"
                )
                page.evaluate("""() => {
                    state.config.automatic_free_models = true;
                    state.authStatuses.set('openai', {state: 'connected', connected: true});
                    state.startup = {startup: {providers: {openai: 'ready'}},
                        cached_models: {openai: ['synthetic-catalog-model']}};
                    state.providerChecks.delete('openai');
                    updateProviderCard(state.config.provider_status.find(
                        provider => provider.provider_id === 'openai'));
                }""")
                openai_card = page.locator('[data-provider="openai"]')
                assert openai_card.locator(".provider-meta").first.inner_text() == (
                    "1 model available"
                )
                policy_note = openai_card.locator('[data-free-policy-note="openai"]')
                assert policy_note.is_visible()
                assert (
                    "not included in automatic free routing" in policy_note.inner_text()
                )
                openai_card.screenshot(path=str(output / "openai-discovery.png"))
                assert not failures, failures
                checks.append(
                    "Chrome connected OpenAI catalog count and separate free-policy exclusion (synthetic state)"
                )
                page.reload()
                page.locator("#providerGroups .provider-strip").first.wait_for(
                    timeout=20000
                )
                free_status = {
                    "automatic": True,
                    "minimum_context_tokens": 512000,
                    "refreshed_at": "2026-09-20T08:00:00+00:00",
                    "eligible_models": 2,
                    "available_models": 1,
                    "note": "Synthetic browser fixture; no real provider credentials.",
                    "providers": [
                        {
                            "provider": "open_router",
                            "name": "OpenRouter",
                            "mode": "zero_price",
                            "state": "COOLDOWN",
                            "models": 1,
                            "available_models": 0,
                            "model_ids": ["synthetic-1m"],
                            "model_details": [
                                {"id": "synthetic-1m", "context_tokens": 1048576}
                            ],
                            "retry_at": "2026-09-21T00:00:00+00:00",
                            "source": "https://openrouter.ai/docs/api/reference/limits",
                        },
                        {
                            "provider": "gemini",
                            "name": "Gemini",
                            "mode": "free_account",
                            "state": "CONFIRM_FREE_ACCOUNT",
                            "models": 1,
                            "available_models": 1,
                            "model_ids": ["synthetic-512k"],
                            "model_details": [
                                {"id": "synthetic-512k", "context_tokens": 512000}
                            ],
                            "account_confirmed": False,
                            "source": "https://ai.google.dev/gemini-api/docs/billing",
                        },
                    ],
                }
                confirmation_requests = []

                def free_fixture(route):
                    if route.request.url.endswith("/accounts/gemini"):
                        assert route.request.post_data_json == {"no_paid_billing": True}
                        assert route.request.headers["x-fcc-admin"] == "1"
                        confirmation_requests.append(True)
                        free_status["providers"][1].update(
                            account_confirmed=True, state="ELIGIBLE"
                        )
                    route.fulfill(json=free_status)

                page.route("**/admin/api/free/**", free_fixture)
                page.get_by_role(
                    "link", name="Automatic free routing", exact=True
                ).click()
                page.get_by_role(
                    "heading", name="Automatic free routing", exact=True
                ).wait_for()
                page.locator("td").filter(has_text="Cooling down").wait_for()
                assert page.locator("#eligible").inner_text() == "2"
                page.get_by_role("checkbox").check()
                page.get_by_text("Eligible", exact=True).wait_for()
                assert confirmation_requests
                page.get_by_role("button", name="Refresh catalogs").click()
                page.locator("#notice").filter(has_text="Catalog checked").wait_for()
                page.locator("details summary").first.click()
                assert (
                    "1,048,576 context tokens"
                    in page.locator("details").first.inner_text()
                )
                page.screenshot(path=str(output / "free-routing.png"), full_page=True)
                assert not failures, failures
                checks.append(
                    "Chrome free routing status, 512k+ display, account confirmation and refresh (synthetic API fixtures)"
                )
                page.goto(url + "/admin")
                page.locator("#providerGroups .provider-strip").first.wait_for(
                    timeout=20000
                )
                page.get_by_role("link", name="Change password", exact=True).click()
                page.locator("#password").fill(password)
                page.locator("#login button").click()
                page.locator("#change").wait_for(state="visible")
                changed = "Synthetic Browser Mountain 839!"
                page.locator("#newPassword").fill(changed)
                page.locator("#confirm").fill(changed)
                page.locator("#change button").click()
                page.locator("#login").wait_for(state="visible")
                page.locator("#password").fill(changed)
                page.locator("#login button").click()
                page.wait_for_url(url + "/admin")
                page.locator("#hardenedLogout").click()
                page.wait_for_url("**/admin/login")
                checks.append("Chrome existing password change and sign-out")
                page.locator("#password").fill(changed)
                page.locator("#login button").click()
                page.wait_for_url(url + "/admin")
                before = store.path.read_bytes()
                AdminAccounts().reset_password("Synthetic Local Reset 925!")
                assert store.path.read_bytes() == before
                assert page.request.get(url + "/admin/api/status").status == 401
                page.reload()
                page.wait_for_url("**/admin/login")
                page.locator("#password").fill("Synthetic Local Reset 925!")
                page.locator("#login button").click()
                page.wait_for_url(url + "/admin")
                checks.append(
                    "local reset revokes browser session and preserves provider config"
                )
                old_instance = page.request.get(url + "/admin/ready").json()[
                    "instance_id"
                ]
                new_url = f"http://127.0.0.1:{unused_port()}"
                allowed_origins.add(new_url)
                changed = page.request.post(
                    url + "/admin/api/config/apply",
                    data={"values": {"PORT": new_url.rsplit(":", 1)[1]}},
                    headers={"X-FCC-Admin": "1", "Origin": url},
                ).json()
                assert changed["applied"] and changed["restart"]["automatic"]
                deadline = time.monotonic() + 40
                with httpx.Client(trust_env=False, timeout=2) as http:
                    while True:
                        try:
                            ready = http.get(
                                new_url + "/admin/ready", headers={"Origin": url}
                            )
                            if (
                                ready.status_code == 200
                                and ready.json().get("status") == "running"
                                and ready.json().get("instance_id") != old_instance
                            ):
                                assert (
                                    ready.headers["Access-Control-Allow-Origin"] == url
                                )
                                break
                        except httpx.HTTPError:
                            pass
                        assert time.monotonic() < deadline, (
                            "Restart did not become ready"
                        )
                        time.sleep(0.2)
                assert store.read(env={}).settings.proxy_auth_token == token
                page.goto(new_url + "/admin")
                page.wait_for_url(new_url + "/admin/login")
                page.locator("#password").fill("Synthetic Local Reset 925!")
                page.locator("#login button").click()
                page.wait_for_url(new_url + "/admin")
                checks.append(
                    "configuration restart on a new port preserves credentials and permits sign-in"
                )
                assert not failures, f"Browser JavaScript errors: {failures}"
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=15)
            process.stdout.close()
            mock.shutdown()
            mock.server_close()
    report = {
        "passed": checks,
        "live_provider_used": False,
        "real_credentials_used": False,
    }
    (ROOT / "security-validation" / "browser-smoke.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
