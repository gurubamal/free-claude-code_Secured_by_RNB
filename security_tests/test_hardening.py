import hashlib
import os
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient

from free_claude_code.api.admin_accounts import COOKIE, AdminSessionMiddleware
from free_claude_code.api.app import create_app
from free_claude_code.api.dependencies import (
    require_anthropic_proxy_auth,
    require_proxy_auth,
)
from free_claude_code.config.loader import ManagedConfigStore, get_settings
from free_claude_code.config.paths import managed_env_path
from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.admin_accounts import AdminAccounts, LoginThrottled
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
    unprotect_text,
)
from free_claude_code.harnesses.environment import client_environment
from free_claude_code.providers.runtime.config import build_provider_config

PASSWORD = "Test-only Purple River 927!"
OTHER = "Test-only Orange Mountain 184!"
HEADERS = {"X-FCC-Admin": "1", "Origin": "http://127.0.0.1"}


@pytest.fixture
def account(tmp_path):
    value = AdminAccounts(tmp_path / "private" / "admin.json")
    value.reset_password(PASSWORD)
    return value


@pytest.fixture
def client(account):
    settings = Settings()
    services = SimpleNamespace(
        requests=SimpleNamespace(current_settings=lambda: settings),
        admin=SimpleNamespace(
            admin_status=AsyncMock(
                return_value={
                    "status": "running",
                    "instance_id": "a" * 32,
                    "host": "127.0.0.1",
                    "port": 8082,
                    "provider_status": [{"secret": "must-not-be-public"}],
                    "cached_models": {},
                }
            )
        ),
    )
    app = create_app(services)
    next(m for m in app.user_middleware if m.cls is AdminSessionMiddleware).kwargs[
        "accounts"
    ] = account
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 43210),
        follow_redirects=False,
    ) as test:
        yield test


def login(client, password=PASSWORD):
    return client.post(
        "/admin/auth/login",
        json={"username": "admin", "password": password},
        headers=HEADERS,
    )


def test_secure_defaults():
    settings = Settings()
    assert settings.host == "127.0.0.1"
    assert settings.proxy_auth_enabled
    assert len(settings.proxy_auth_token) >= 32
    assert settings.proxy_auth_token != Settings().proxy_auth_token
    assert settings.messaging_platform == "none"


@pytest.mark.parametrize(
    "values",
    [
        {"HOST": "0.0.0.0"},
        {"HOST": "192.168.1.2"},
        {"HOST": "example.com"},
        {"PROXY_AUTH_ENABLED": "false"},
        {"ANTHROPIC_AUTH_TOKEN": "freecc"},
        {"ANTHROPIC_AUTH_TOKEN": "a" * 64},
    ],
)
def test_unsafe_settings_fail_closed(values):
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_dpapi_roundtrip_and_tamper(tmp_path):
    target = tmp_path / "private" / "secret"
    value = "synthetic provider credential — " + secrets.token_hex(20)
    atomic_write_private_text(target, value)
    assert read_private_text(target) == value
    disk = target.read_text()
    if os.name == "nt":
        assert disk.startswith("FCC-DPAPI-V1:") and value not in disk
        with pytest.raises(OSError):
            unprotect_text("FCC-DPAPI-V1:" + "A" * 100)
    assert not list(target.parent.glob("*.tmp"))


def test_first_launch_unique_and_not_repeated(tmp_path):
    one = AdminAccounts(tmp_path / "one" / "admin.json")
    two = AdminAccounts(tmp_path / "two" / "admin.json")
    temporary = one.initialize()
    assert temporary and temporary != two.initialize()
    assert one.initialize() is None
    assert one.initial_password() == temporary
    token, required = one.login("admin", temporary)
    assert required
    assert temporary not in one.path.read_text()
    assert one.change_password(token, temporary, PASSWORD)
    assert not one.path.with_name("initial-password").exists()
    assert one.session(token) is None
    assert one.login("admin", PASSWORD)[1] is False


def test_persistent_proxy_and_provider_survive_reset(account):
    store = ManagedConfigStore()
    store.initialize(env={})
    original = store.read(env={})
    values = dict(original.managed)
    values["OPENROUTER_API_KEY"] = "synthetic-provider-key-only"
    store.commit(values)
    before = managed_env_path().read_bytes()
    account.reset_password(OTHER)
    store.initialize(env={})
    assert get_settings().proxy_auth_token == original.settings.proxy_auth_token
    assert (
        store.read(env={}).settings.open_router_api_key == "synthetic-provider-key-only"
    )
    assert managed_env_path().read_bytes() == before
    assert b"synthetic-provider-key-only" not in before if os.name == "nt" else True


def test_reset_revokes_sessions_across_account_instances(account):
    token, _ = account.login("admin", PASSWORD)
    assert account.session(token)
    AdminAccounts(account.path).reset_password(OTHER)
    assert account.session(token) is None
    assert account.login("admin", PASSWORD) is None
    assert account.login("admin", OTHER)


def test_reset_recovers_corrupt_account(account):
    account.path.write_text("corrupt")
    account.reset_password(OTHER)
    assert account.login("admin", OTHER)


def test_bad_password_never_replaces_account(account):
    before = account.path.read_bytes()
    with pytest.raises(ValueError):
        account.reset_password("admin")
    assert account.path.read_bytes() == before


def test_throttle_and_expiry(account):
    for _ in range(5):
        assert account.login("admin", "wrong") is None
    with pytest.raises(LoginThrottled):
        account.login("admin", PASSWORD)
    account.reset_password(PASSWORD)
    token, _ = account.login("admin", PASSWORD)
    key = hashlib.sha256(token.encode()).hexdigest()
    revision, _ = account.sessions[key]
    account.sessions[key] = revision, time.time() - 1
    assert account.session(token) is None


@pytest.mark.parametrize(
    "path", ["/admin/api/status", "/admin/api/config", "/admin/api/code/sessions"]
)
def test_admin_unauthenticated(client, path):
    assert client.get(path).status_code == 401


def test_admin_page_login_logout(client):
    assert client.get("/admin").status_code == 303
    assert client.get("/admin/login").status_code == 200
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/api/status").status_code == 200
    assert (
        client.post("/admin/auth/logout", json={}, headers=HEADERS).status_code == 200
    )
    assert client.get("/admin/api/status").status_code == 401


def test_first_login_cannot_skip_change(client, account):
    account.path.unlink()
    temporary = account.initialize()
    assert login(client, temporary).json()["password_change_required"]
    assert client.get("/admin/api/status").status_code == 403
    result = client.post(
        "/admin/auth/password",
        json={"current_password": temporary, "new_password": PASSWORD},
        headers=HEADERS,
    )
    assert result.status_code == 200
    assert login(client).status_code == 200
    assert client.get("/admin/api/status").status_code == 200


def test_change_requires_current_password_and_revokes_cookie(client):
    assert login(client).status_code == 200
    token = client.cookies.get(COOKIE)
    result = client.post(
        "/admin/auth/password",
        json={"current_password": "wrong", "new_password": OTHER},
        headers=HEADERS,
    )
    assert result.status_code == 401
    assert (
        client.post(
            "/admin/auth/password",
            json={"current_password": PASSWORD, "new_password": OTHER},
            headers=HEADERS,
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/admin/api/status", headers={"Cookie": f"{COOKIE}={token}"}
        ).status_code
        == 401
    )
    assert login(client, OTHER).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-FCC-Admin": "1", "Origin": "http://evil.example"},
        {"X-FCC-Admin": "1", "Origin": "http://127.0.0.1:9999"},
        {"X-FCC-Admin": "1", "Host": "evil.example"},
    ],
)
def test_login_csrf_and_rebinding_rejected(client, headers):
    assert (
        client.post(
            "/admin/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers=headers,
        ).status_code
        == 403
    )


def test_remote_peer_rejected(client):
    with TestClient(
        client.app, base_url="http://127.0.0.1", client=("10.0.0.1", 1000)
    ) as remote:
        assert remote.get("/admin/login").status_code == 403


def test_public_readiness_only_safe_fields(client):
    response = client.get("/admin/ready", headers={"Origin": "http://127.0.0.1:9999"})
    assert response.status_code == 200
    assert set(response.json()) == {"status", "instance_id", "host", "port"}
    assert "must-not-be-public" not in response.text
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert (
        client.get(
            "/admin/ready", headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )


def test_bounded_login_body_and_throttle(client):
    assert (
        client.post(
            "/admin/auth/login",
            content="x" * 9000,
            headers={**HEADERS, "Content-Type": "application/json"},
        ).status_code
        == 413
    )
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    assert login(client).status_code == 429


def request_with(headers):
    return Request(
        {
            "type": "http",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_proxy_authentication_is_independent(client):
    assert login(client).status_code == 200
    assert client.head("/v1/messages").status_code == 401
    settings = Settings()
    require_proxy_auth(
        request_with({"authorization": "Bearer " + settings.proxy_auth_token}), settings
    )
    require_anthropic_proxy_auth(
        request_with({"x-api-key": settings.proxy_auth_token}), settings
    )
    with pytest.raises(HTTPException):
        require_proxy_auth(request_with({"authorization": "Bearer freecc"}), settings)
    unsafe = settings.model_copy(update={"proxy_auth_enabled": False})
    with pytest.raises(HTTPException) as error:
        require_proxy_auth(request_with({}), unsafe)
    assert error.value.status_code == 503


def test_parent_secrets_not_given_to_client():
    child = client_environment(
        {
            "PATH": "native",
            "INDSTOCKS_TOKEN": "sentinel",
            "AWS_SECRET_ACCESS_KEY": "sentinel",
            "ANTHROPIC_API_KEY": "sentinel",
            "PYTHONPATH": "sentinel",
        },
        proxy_root_url="http://127.0.0.1:8082",
    )
    assert child["PATH"] == "native"
    assert "sentinel" not in child.values()


@pytest.mark.parametrize("session_id", [None, "existing-session"])
def test_remote_managed_claude_restricted(session_id):
    from free_claude_code.cli.managed.claude import build_managed_claude_command

    command = build_managed_claude_command(
        claude_bin="claude",
        prompt="review",
        session_id=session_id,
        fork_session=False,
        allowed_dirs=["C:/work"],
    )
    assert "--dangerously-skip-permissions" not in command
    assert "--restricted" in command and "--strict-mcp-config" in command
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"


def test_telegram_missing_allowlist_fails_closed():
    from free_claude_code.messaging.platforms.telegram_inbound import (
        telegram_text_message_from_update,
        telegram_voice_request_from_update,
    )

    update = SimpleNamespace(
        message=SimpleNamespace(text="hello", voice=object()),
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
    )
    assert (
        telegram_text_message_from_update(
            update, allowed_user_id=None, log_raw_messaging_content=False
        )
        is None
    )
    assert (
        telegram_voice_request_from_update(update, None, allowed_user_id=None) is None
    )


def test_native_permission_defaults_cannot_disable_sandbox():
    from free_claude_code.application.code_sessions import CodeValidationError
    from free_claude_code.runtime.codex_app_server import _permission_settings

    dangerous = {
        "approvalPolicy": "never",
        "activePermissionProfile": {"id": ":danger-full-access"},
    }
    assert _permission_settings("ask", dangerous)["permissions"] == ":workspace"
    with pytest.raises(CodeValidationError):
        _permission_settings("full_access", dangerous)


@pytest.mark.parametrize(
    "name", ["auto", "claude-opus", "open_router/paid/model", "openai/gpt-paid"]
)
def test_free_mode_overrides_all_client_model_choices(name):
    from free_claude_code.application.routing import ModelRouter

    route = ModelRouter(Settings(model_fallbacks=("openai/paid",))).resolve(name)
    assert route.primary.provider_model_ref == "open_router/openrouter/free"
    assert route.fallbacks == ()


def test_free_cost_guard_cannot_be_overridden():
    from free_claude_code.config.free_mode import free_request_body
    from free_claude_code.providers.open_router.client import (
        _PROFILE,
        OpenRouterChatBehavior,
    )

    original = {
        "model": "openrouter/free",
        "messages": [{"role": "user", "content": "hello"}],
        "extra_body": {
            "provider": {"max_price": {"prompt": 999}},
            "models": ["paid/model"],
            "plugins": [{"id": "web"}],
        },
        "models": ["paid/model"],
        "plugins": [{"id": "web"}],
        "max_tokens": 90000,
    }
    guarded = free_request_body(original)
    assert guarded["provider"]["max_price"] == {
        "prompt": 0,
        "completion": 0,
        "request": 0,
        "image": 0,
    }
    assert guarded["max_tokens"] == 8192
    assert all(key not in guarded for key in ("plugins", "models", "extra_body"))
    sdk = OpenRouterChatBehavior(_PROFILE).prepare_create_body(original)
    assert "provider" not in sdk
    assert sdk["extra_body"] == {"provider": guarded["provider"]}
    assert original["max_tokens"] == 90000


def test_catalog_only_advertises_automatic_route():
    from free_claude_code.application.model_catalog import read_model_catalog

    catalog = read_model_catalog(SimpleNamespace(current_settings=lambda: Settings()))
    assert len(catalog.models) == 1
    assert catalog.default_model_id == "open_router/openrouter/free"


def test_chat_completions_requires_proxy_token(client):
    assert (
        client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "url,valid",
    [
        ("http://example.com/v1", False),
        ("https://user:pass@example.com/v1", False),
        ("https://example.com/v1?key=secret", False),
        ("https://example.com/v1", True),
        ("http://127.0.0.1:1234/v1", True),
    ],
)
def test_provider_transport(url, valid):
    from dataclasses import replace

    from free_claude_code.application.errors import ApplicationUnavailableError

    descriptor = replace(
        PROVIDER_CATALOG["lmstudio"], default_base_url=url, base_url_attr=None
    )
    if valid:
        assert build_provider_config(descriptor, Settings()).base_url == url
    else:
        with pytest.raises(ApplicationUnavailableError):
            build_provider_config(descriptor, Settings())
