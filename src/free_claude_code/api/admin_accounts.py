"""Session and CSRF boundary around every Admin and browser coding endpoint."""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from free_claude_code.core.admin_accounts import (
    SESSION_SECONDS,
    AdminAccounts,
    LoginThrottled,
)

from .admin_security import require_loopback_admin

COOKIE = "fcc_hardened_session"
AUTH_PATHS = {"/admin/auth/login", "/admin/auth/logout", "/admin/auth/password"}


class AdminSessionMiddleware:
    def __init__(
        self, app, accounts: AdminAccounts | None = None, status_provider=None
    ):
        self.app = app
        self.accounts = accounts or AdminAccounts()
        self.status_provider = status_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (
            scope.get("path") == "/admin" or scope.get("path", "").startswith("/admin/")
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            require_loopback_admin(request)
            # Public readiness carries no provider, account or configuration data.
            # Local cross-port polling lets an authenticated settings change reconnect.
            if request.url.path == "/admin/ready" and request.method == "GET":
                status = await self.status_provider() if self.status_provider else {}
                response = JSONResponse(
                    {
                        key: status.get(key)
                        for key in ("instance_id", "status", "host", "port")
                    }
                )
                if request.headers.get("origin"):
                    response.headers["Access-Control-Allow-Origin"] = request.headers[
                        "origin"
                    ]
                    response.headers["Vary"] = "Origin"
                response.headers["Cache-Control"] = "no-store"
                await response(scope, receive, send)
                return
            # A local page on another port is not the Admin's origin.
            origin = request.headers.get("origin")
            if origin and (
                urlsplit(origin).netloc.lower()
                != request.headers.get("host", "").lower()
                or urlsplit(origin).scheme != request.url.scheme
            ):
                raise HTTPException(403, "Admin origin mismatch")
            response = await self._handle(request)
        except HTTPException as error:
            response = JSONResponse(
                {"detail": error.detail}, status_code=error.status_code
            )
        except OSError, ValueError, KeyError:
            response = JSONResponse(
                {
                    "detail": "Admin account unavailable. Run the local reset-password command."
                },
                status_code=503,
            )
        if response is not None:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            await response(scope, receive, send)
        else:

            async def secure_send(message):
                if message["type"] == "http.response.start":
                    message["headers"] = [
                        *message.get("headers", []),
                        (b"x-frame-options", b"DENY"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                await send(message)

            await self.app(scope, receive, secure_send)

    async def _handle(self, request):
        path = request.url.path
        if request.method not in {
            "GET",
            "HEAD",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
        }:
            raise HTTPException(405, "Method not allowed")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("x-fcc-admin") != "1"
        ):
            raise HTTPException(403, "Admin request header required")
        if path == "/admin/login":
            if request.method != "GET":
                raise HTTPException(405, "Method not allowed")
            return HTMLResponse(
                Path(__file__)
                .with_name("admin_static")
                .joinpath("login.html")
                .read_text(encoding="utf-8")
            )
        token = request.cookies.get(COOKIE, "")
        if path in AUTH_PATHS:
            if request.method != "POST":
                raise HTTPException(405, "Method not allowed")
            if (
                request.headers.get("content-type", "").split(";")[0]
                != "application/json"
            ):
                raise HTTPException(415, "JSON required")
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 8192:
                    raise HTTPException(413, "Request too large")
            try:
                body = json.loads(raw)
            except ValueError, UnicodeError:
                raise HTTPException(400, "Invalid JSON") from None
            if not isinstance(body, dict):
                raise HTTPException(400, "JSON object required")
            if path.endswith("/login"):
                if not all(
                    isinstance(body.get(k), str) for k in ("username", "password")
                ):
                    raise HTTPException(400, "Username and password required")
                try:
                    result = await asyncio.to_thread(
                        self.accounts.login, body["username"], body["password"]
                    )
                except LoginThrottled:
                    return JSONResponse(
                        {
                            "detail": "Too many attempts. Wait one minute or reset locally."
                        },
                        429,
                        headers={"Retry-After": "60"},
                    )
                if result is None:
                    raise HTTPException(401, "Invalid username or password")
                token, temporary = result
                response = JSONResponse(
                    {"ok": True, "password_change_required": temporary}
                )
                response.set_cookie(
                    COOKIE,
                    token,
                    httponly=True,
                    samesite="strict",
                    secure=request.url.scheme == "https",
                    max_age=SESSION_SECONDS,
                    path="/admin",
                )
                return response
            record = await asyncio.to_thread(self.accounts.session, token)
            if record is None:
                raise HTTPException(401, "Sign in required")
            if path.endswith("/password"):
                if not all(
                    isinstance(body.get(k), str)
                    for k in ("current_password", "new_password")
                ):
                    raise HTTPException(400, "Current and new passwords required")
                try:
                    changed = await asyncio.to_thread(
                        self.accounts.change_password,
                        token,
                        body["current_password"],
                        body["new_password"],
                    )
                except ValueError as error:
                    raise HTTPException(400, str(error)) from None
                if not changed:
                    raise HTTPException(401, "Current password is incorrect")
            else:
                await asyncio.to_thread(self.accounts.logout, token)
            response = JSONResponse({"ok": True})
            response.delete_cookie(COOKIE, path="/admin")
            return response
        record = await asyncio.to_thread(self.accounts.session, token)
        if record is None or record["temporary"]:
            if request.method == "GET" and not path.startswith("/admin/api/"):
                return RedirectResponse("/admin/login", status_code=303)
            raise HTTPException(
                401 if record is None else 403,
                "Sign in and change the temporary password",
            )
        return None
