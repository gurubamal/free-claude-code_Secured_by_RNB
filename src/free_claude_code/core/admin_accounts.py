"""Single-user local Admin account, password hashing, reset and expiring sessions."""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from functools import wraps
from pathlib import Path

from free_claude_code.config.paths import config_dir_path
from free_claude_code.core.interprocess_lock import InterprocessFileLock
from free_claude_code.core.private_storage import (
    atomic_write_private_text,
    read_private_text,
)

USERNAME = "admin"
ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60


class LoginThrottled(Exception):
    pass


def synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return locked


def validate_password(password: str) -> None:
    if len(password) < 14 or len(password) > 1024 or len(set(password)) < 8:
        raise ValueError("Use 14-1024 characters with at least 8 distinct characters")


class AdminAccounts:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir_path() / "auth" / "admin.json"
        self._mutex = threading.RLock()
        self.sessions: dict[str, tuple[str, float]] = {}
        self.failures: deque[float] = deque()

    def _read(self) -> dict:
        record = json.loads(read_private_text(self.path))
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or record.get("iterations") != ITERATIONS
            or record.get("username") != USERNAME
            or not isinstance(record.get("temporary"), bool)
            or not all(
                isinstance(record.get(key), str)
                for key in ("salt", "digest", "revision")
            )
        ):
            raise ValueError(
                "Unsupported Admin credential record; use local password reset"
            )
        return record

    @synchronized
    def initialize(self) -> str | None:
        lock = InterprocessFileLock(self.path.with_suffix(".lock"))
        if not lock.acquire(wait=True, timeout=10):
            raise OSError("Admin account is busy")
        try:
            if self.path.exists():
                self._read()
                return None
            password = secrets.token_urlsafe(24)
            self._write_password(password, temporary=True)
            atomic_write_private_text(self.path.with_name("initial-password"), password)
            return password
        finally:
            lock.release()

    def _write_password(self, password: str, *, temporary: bool) -> None:
        validate_password(password)
        salt = secrets.token_bytes(32)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
        record = {
            "version": 1,
            "username": USERNAME,
            "iterations": ITERATIONS,
            "salt": base64.b64encode(salt).decode(),
            "digest": base64.b64encode(digest).decode(),
            "revision": secrets.token_hex(24),
            "temporary": temporary,
        }
        atomic_write_private_text(self.path, json.dumps(record))
        if not temporary:
            self.path.with_name("initial-password").unlink(missing_ok=True)
        self.sessions.clear()

    @synchronized
    def initial_password(self) -> str:
        record = self._read()
        if not record["temporary"]:
            raise ValueError(
                "The temporary password has already been replaced. Use reset-password if needed."
            )
        value = read_private_text(self.path.with_name("initial-password"))
        if not self._password_matches(value, record):
            raise ValueError("Initial password is unavailable. Use reset-password.")
        return value

    @synchronized
    def reset_password(self, password: str) -> None:
        """Local OS-user recovery; only the Admin record changes."""
        lock = InterprocessFileLock(self.path.with_suffix(".lock"))
        if not lock.acquire(wait=True, timeout=10):
            raise OSError("Admin account is busy")
        try:
            self._write_password(password, temporary=False)
            self.failures.clear()
        finally:
            lock.release()

    def _password_matches(self, password: str, record: dict) -> bool:
        if not isinstance(password, str) or len(password) > 1024:
            return False
        salt = base64.b64decode(record["salt"], validate=True)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
        return hmac.compare_digest(
            digest, base64.b64decode(record["digest"], validate=True)
        )

    @synchronized
    def login(self, username: str, password: str) -> tuple[str, bool] | None:
        now = time.monotonic()
        while self.failures and self.failures[0] < now - 60:
            self.failures.popleft()
        if len(self.failures) >= 5:
            raise LoginThrottled
        record = self._read()
        valid_password = self._password_matches(password, record)
        if username != USERNAME or not valid_password:
            self.failures.append(now)
            return None
        self.failures.clear()
        token = secrets.token_urlsafe(32)
        self.sessions = {k: v for k, v in self.sessions.items() if v[1] > time.time()}
        if len(self.sessions) >= 128:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (
            record["revision"],
            time.time() + SESSION_SECONDS,
        )
        return token, bool(record["temporary"])

    @synchronized
    def session(self, token: str) -> dict | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        session = self.sessions.get(key)
        if not session:
            return None
        record = self._read()
        if session[0] != record["revision"] or session[1] <= time.time():
            self.sessions.pop(key, None)
            return None
        return record

    @synchronized
    def logout(self, token: str) -> None:
        self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    @synchronized
    def change_password(self, token: str, current: str, new: str) -> bool:
        lock = InterprocessFileLock(self.path.with_suffix(".lock"))
        if not lock.acquire(wait=True, timeout=10):
            raise OSError("Admin account is busy")
        try:
            record = self.session(token)
            if record is None or not self._password_matches(current, record):
                return False
            self._write_password(new, temporary=False)
            self.failures.clear()
            return True
        finally:
            lock.release()
