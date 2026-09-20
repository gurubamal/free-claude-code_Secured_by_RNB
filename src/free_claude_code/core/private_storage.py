"""Private text storage. Windows ciphertext is bound to the current user's DPAPI.

This protects copied files at rest, not a compromised process running as that user.
POSIX uses owner-only files; encryption on other platforms is not claimed.
"""

import base64
import ctypes
import os
import tempfile
from pathlib import Path

_PREFIX = "FCC-DPAPI-V1:"


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is required to open this credential store")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN. Deliberately omit CRYPTPROTECT_LOCAL_MACHINE.
    if not function(
        ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)
    ):
        raise OSError("Windows credential protection failed")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def protect_text(text: str) -> str:
    if os.name != "nt":
        return text
    return _PREFIX + base64.b64encode(
        _dpapi(text.encode("utf-8"), decrypt=False)
    ).decode("ascii")


def unprotect_text(text: str) -> str:
    if not text.startswith(_PREFIX):
        return text
    try:
        ciphertext = base64.b64decode(text[len(_PREFIX) :].strip(), validate=True)
        return _dpapi(ciphertext, decrypt=True).decode("utf-8")
    except (ValueError, UnicodeError, OSError) as error:
        raise OSError(
            "Credential store cannot be decrypted by this Windows user"
        ) from error


def read_private_text(path: Path) -> str:
    return unprotect_text(path.read_text(encoding="utf-8"))


def atomic_write_private_text(path: Path, text: str) -> None:
    # Protect before creating any file, so failures never leave plaintext behind.
    content = protect_text(text)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError("Refusing a symlink credential file")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
