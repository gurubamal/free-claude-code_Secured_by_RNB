"""FCC-owned Google OAuth credentials, refresh, cancellation and revocation."""

import asyncio
import json
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path

import httpx

from free_claude_code.application.connected_accounts import (
    ConnectedAccountLoginMode,
    ConnectedAccountState,
    ConnectedAccountStatus,
)
from free_claude_code.config.paths import config_dir_path
from free_claude_code.core.interprocess_lock import InterprocessFileLock
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
)

from .login import (
    LOGIN_SECONDS,
    REVOKE_URL,
    SCOPE,
    TOKEN_URL,
    GoogleBrowserLogin,
    GoogleLoginError,
)


class GeminiOAuthManager:
    provider_id = "gemini_oauth"

    def __init__(
        self, settings_provider, *, credential_path: Path | None = None, client=None
    ):
        self._settings = settings_provider
        self._path = credential_path or config_dir_path() / "auth" / "gemini-oauth.json"
        self._client = client or httpx.AsyncClient(
            timeout=20, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._mutex = asyncio.Lock()
        self._task = None
        self._browser = None
        self._login_lock = None
        self._attempt = None
        self._expires = None
        self._error = None
        self._closed = False
        self._revision = 0
        self._credentials = None
        try:
            self._credentials = self._read()
        except OSError, ValueError:
            self._error = "Saved Google credentials could not be read. Disconnect and sign in again."
        if self._credentials:
            self._revision = self._credentials.get("revision", 1)

    def _configuration(self):
        settings = self._settings()
        client_id = settings.gemini_oauth_client_id or ""
        secret = settings.gemini_oauth_client_secret or ""
        project = settings.gemini_oauth_project_id or ""
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id)
            or not secret
        ):
            raise GoogleLoginError(
                "Configure your own Google Desktop OAuth client ID and secret in Edit, save, then sign in."
            )
        if not re.fullmatch(r"(?:[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]{6,30})", project):
            raise GoogleLoginError(
                "Set a valid Google Cloud project ID in Edit and enable its Generative Language API."
            )
        return {"client_id": client_id, "client_secret": secret, "project_id": project}

    def _read(self):
        if not self._path.exists():
            return None
        record = json.loads(read_private_text(self._path))
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ValueError("Invalid Google credential record")
        for key in (
            "access_token",
            "refresh_token",
            "client_id",
            "client_secret",
            "project_id",
        ):
            if not isinstance(record.get(key), str) or not record[key]:
                raise ValueError("Invalid Google credential record")
        if type(record.get("expires_at")) is not int:
            raise ValueError("Invalid Google credential expiry")
        return record

    def _matches_configuration(self):
        try:
            config = self._configuration()
        except GoogleLoginError:
            return False
        return self._credentials is not None and all(
            self._credentials.get(k) == v for k, v in config.items()
        )

    def is_connected(self):
        return not self._closed and self._matches_configuration()

    def connected_provider_ids(self):
        return (self.provider_id,) if self.is_connected() else ()

    def status(self):
        connecting = self._task is not None and not self._task.done()
        connected = self.is_connected()
        message = self._error
        if not connecting and not connected and not message:
            try:
                self._configuration()
                message = (
                    "Settings changed. Disconnect and sign in again."
                    if self._credentials
                    else "Sign in with Google. Your project's Gemini API quotas and billing apply."
                )
            except GoogleLoginError as error:
                message = str(error)
        return ConnectedAccountStatus(
            provider_id=self.provider_id,
            state=ConnectedAccountState.CONNECTING
            if connecting
            else ConnectedAccountState.ERROR
            if self._error
            else ConnectedAccountState.CONNECTED
            if connected
            else ConnectedAccountState.DISCONNECTED,
            connected=connected,
            revision=self._revision,
            attempt_id=self._attempt if connecting else None,
            authorization_url=self._browser.authorization_url
            if connecting and self._browser
            else None,
            mode=ConnectedAccountLoginMode.BROWSER if connecting else None,
            expires_at=self._expires if connecting else None,
            display_identity=f"Google Cloud project: {self._credentials['project_id']}"
            if connected
            else None,
            message=message,
            supported_login_modes=(ConnectedAccountLoginMode.BROWSER,),
        )

    def _file_lock(self):
        lock = InterprocessFileLock(self._path.with_suffix(".lock"))
        if not lock.acquire():
            raise GoogleLoginError(
                "Google account credentials are busy in another process. Try again."
            )
        return lock

    async def start_login(self, mode):
        async with self._mutex:
            if self._closed:
                raise GoogleLoginError("Google account manager is closed.")
            if self._task and not self._task.done():
                return self.status()
            try:
                if mode != ConnectedAccountLoginMode.BROWSER:
                    raise GoogleLoginError("Use browser sign-in for Google.")
                configuration = self._configuration()
                lock = self._file_lock()
                try:
                    self._browser = await GoogleBrowserLogin.start(
                        configuration["client_id"]
                    )
                except BaseException:
                    lock.release()
                    raise
                self._error = None
                self._attempt = secrets.token_hex(16)
                self._expires = int(time.time()) + LOGIN_SECONDS
                self._login_lock = lock
                self._task = asyncio.create_task(
                    self._complete_login(configuration, lock)
                )
            except (GoogleLoginError, OSError) as error:
                self._error = (
                    str(error)
                    if isinstance(error, GoogleLoginError)
                    else "Could not start the local Google sign-in listener."
                )
            return self.status()

    async def _tokens(self, fields):
        response = await self._client.post(TOKEN_URL, data=fields)
        try:
            payload = response.json()
        except ValueError:
            raise GoogleLoginError(
                "Google returned an unreadable authorization response. Try again."
            ) from None
        if not response.is_success:
            if isinstance(payload, dict) and payload.get("error") == "invalid_grant":
                raise _RevokedGrant(
                    "Google authorization expired or was revoked. Sign in again."
                )
            raise GoogleLoginError(
                f"Google authorization failed (HTTP {response.status_code}). Check your OAuth client configuration and retry."
            )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("access_token"), str)
            or not payload["access_token"]
            or str(payload.get("token_type", "")).lower() != "bearer"
        ):
            raise GoogleLoginError("Google did not return a usable access token.")
        if (
            type(payload.get("expires_in")) is not int
            or not 0 < payload["expires_in"] <= 86400
        ):
            raise GoogleLoginError("Google did not return a usable token expiry.")
        if "scope" in payload and SCOPE not in str(payload["scope"]).split():
            raise GoogleLoginError(
                "Google did not grant the requested Gemini API scope."
            )
        return payload

    async def _complete_login(self, configuration, lock):
        browser = self._browser
        try:
            grant = await browser.wait()
            payload = await self._tokens(
                {
                    "grant_type": "authorization_code",
                    "client_id": configuration["client_id"],
                    "client_secret": configuration["client_secret"],
                    "redirect_uri": grant.redirect_uri,
                    "code_verifier": grant.verifier,
                    "code": grant.code,
                }
            )
            if (
                not isinstance(payload.get("refresh_token"), str)
                or not payload["refresh_token"]
            ):
                raise GoogleLoginError(
                    "Google did not grant offline access. Sign in again and approve consent."
                )
            if configuration != self._configuration() or self._closed:
                raise GoogleLoginError(
                    "Settings changed during sign-in. Start a new Google sign-in."
                )
            record = {
                "version": 1,
                "revision": secrets.randbits(48),
                **configuration,
                "access_token": payload["access_token"],
                "refresh_token": payload["refresh_token"],
                "expires_at": int(time.time()) + payload["expires_in"],
            }
            atomic_write_private_text(self._path, json.dumps(record))
            self._credentials = record
            self._revision = record["revision"]
        except GoogleLoginError as error:
            self._error = str(error)
        except httpx.HTTPError, OSError, ValueError:
            self._error = (
                "Google sign-in could not complete. Check the connection and retry."
            )
        finally:
            try:
                await browser.close()
                self._browser = None
            finally:
                lock.release()
                self._login_lock = None

    async def access_token(self, *, project_id=None):
        async with self._mutex:
            # A different gateway process may have disconnected this account.
            previous = self._credentials
            try:
                self._credentials = self._read()
            except OSError, ValueError:
                self._credentials = None
                self._revision += 1
                raise GoogleLoginError(
                    "Saved Google credentials are unavailable. Sign in again."
                ) from None
            if previous is not None and self._credentials is None:
                self._revision += 1
            elif self._credentials:
                self._revision = self._credentials.get("revision", self._revision)
            if not self.is_connected():
                raise GoogleLoginError(
                    "Connect Gemini / Google account in Admin first."
                )
            if project_id is not None and self._credentials["project_id"] != project_id:
                raise GoogleLoginError(
                    "Google project changed. Retry with the current provider configuration."
                )
            if self._credentials["expires_at"] > time.time() + 120:
                return self._credentials["access_token"]
            lock = self._file_lock()
            try:
                self._credentials = self._read()
                if not self.is_connected():
                    raise GoogleLoginError(
                        "Google account was disconnected or its settings changed. Sign in again."
                    )
                if self._credentials["expires_at"] > time.time() + 120:
                    return self._credentials["access_token"]
                old = self._credentials
                payload = await self._tokens(
                    {
                        "grant_type": "refresh_token",
                        "client_id": old["client_id"],
                        "client_secret": old["client_secret"],
                        "refresh_token": old["refresh_token"],
                    }
                )
                updated = {
                    **old,
                    "access_token": payload["access_token"],
                    "refresh_token": payload.get("refresh_token")
                    or old["refresh_token"],
                    "expires_at": int(time.time()) + payload["expires_in"],
                }
                atomic_write_private_text(self._path, json.dumps(updated))
                self._credentials = updated
                self._error = None
                return updated["access_token"]
            except _RevokedGrant as error:
                self._path.unlink(missing_ok=True)
                self._credentials = None
                self._revision += 1
                self._error = str(error)
                raise
            except httpx.HTTPError:
                raise GoogleLoginError(
                    "Google token refresh is temporarily unavailable. Try again."
                ) from None
            finally:
                lock.release()

    async def _cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        # A task cancelled before its first instruction never runs its finally block.
        try:
            if self._browser:
                await self._browser.close()
                self._browser = None
        finally:
            if self._login_lock:
                self._login_lock.release()
                self._login_lock = None
        self._task = None
        self._attempt = None
        self._expires = None

    async def cancel_login(self):
        async with self._mutex:
            await self._cancel()
            self._error = None
            return self.status()

    async def disconnect(self):
        async with self._mutex:
            await self._cancel()
            lock = self._file_lock()
            try:
                old = self._credentials
                self._path.unlink(missing_ok=True)
                self._credentials = None
                self._revision += 1
                self._error = None
                if old:
                    try:
                        response = await self._client.post(
                            REVOKE_URL, data={"token": old["refresh_token"]}
                        )
                        if not response.is_success:
                            self._error = "Disconnected locally. Google revocation was not confirmed; remove this app in your Google account connections."
                    except httpx.HTTPError:
                        self._error = "Disconnected locally. Google revocation was not confirmed; remove this app in your Google account connections."
            finally:
                lock.release()
            return self.status()

    async def close(self):
        async with self._mutex:
            await self._cancel()
            self._closed = True
            if self._owns_client:
                await self._client.aclose()


class _RevokedGrant(GoogleLoginError):
    pass
