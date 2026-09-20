"""Desktop OAuth with PKCE and a temporary loopback callback; no CLI credentials."""

import asyncio
import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

from aiohttp import web

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
LOGIN_SECONDS = 600


class GoogleLoginError(ValueError):
    """An intentionally credential-free message safe for Admin."""


@dataclass(frozen=True, repr=False)
class Grant:
    code: str
    redirect_uri: str
    verifier: str


class GoogleBrowserLogin:
    def __init__(self, runner, result, url, redirect_uri):
        self._runner = runner
        self._result = result
        self.authorization_url = url
        self.redirect_uri = redirect_uri

    @classmethod
    async def start(cls, client_id: str):
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        result = asyncio.get_running_loop().create_future()
        app = web.Application(client_max_size=4096)
        redirect_uri = ""
        expected_host = ""

        def reply(text, status=200):
            return web.Response(
                text=text,
                status=status,
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        async def callback(request):
            if request.host != expected_host or request.remote != "127.0.0.1":
                return reply("Invalid callback host.", 400)
            if any(
                len(request.query.getall(k, [])) > 1 for k in ("state", "code", "error")
            ):
                return reply("Invalid callback parameters.", 400)
            supplied_state = request.query.get("state", "")
            if not secrets.compare_digest(supplied_state.encode(), state.encode()):
                return reply("State mismatch.", 400)
            if result.done():
                return reply("This sign-in callback has already been used.", 409)
            if request.query.get("error"):
                result.set_exception(
                    GoogleLoginError(
                        "Google sign-in was declined. Try signing in again."
                    )
                )
                return reply("Sign-in was not completed. Return to Admin.", 400)
            code = request.query.get("code", "")
            if not code or len(code) > 4096:
                return reply("Missing or invalid authorization code.", 400)
            result.set_result(Grant(code, redirect_uri, verifier))
            return reply(
                "Authorization received. Return to Admin to check whether the connection completed. You can close this tab."
            )

        app.router.add_get("/oauth/callback", callback)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            port = runner.addresses[0][1]
            expected_host = f"127.0.0.1:{port}"
            redirect_uri = f"http://{expected_host}/oauth/callback"
            url = (
                AUTHORIZE_URL
                + "?"
                + urlencode(
                    {
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "scope": SCOPE,
                        "access_type": "offline",
                        "prompt": "consent select_account",
                        "state": state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    }
                )
            )
            return cls(runner, result, url, redirect_uri)
        except BaseException:
            await runner.cleanup()
            raise

    async def wait(self):
        try:
            return await asyncio.wait_for(self._result, LOGIN_SECONDS)
        except TimeoutError:
            raise GoogleLoginError(
                "Google sign-in timed out. Try signing in again."
            ) from None

    async def close(self):
        if not self._result.done():
            self._result.cancel()
        else:
            # Retrieve a declined callback even if cancellation won the race.
            if not self._result.cancelled():
                self._result.exception()
        await self._runner.cleanup()
