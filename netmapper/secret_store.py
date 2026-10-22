"""Keep SNMP secrets out of config.toml in plain text.

Two forms are understood wherever a secret goes in the config:

* ``env:NAME`` reads the environment variable NAME.
* ``dpapi:BASE64`` (Windows only) is a value encrypted with the Windows Data
  Protection API. Only the same Windows user on the same PC can decrypt it, so the
  config file can be read by others, or copied off the PC, without giving the secret
  away. ``netmapper snmp-setup`` writes secrets this way on Windows.
"""

from __future__ import annotations

import base64
import os
import sys

# Mixed into the encryption so other programs using DPAPI for the same user can't
# decrypt netmapper's values by accident.
_ENTROPY = b"netmapper-snmp-secret"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01

DPAPI_AVAILABLE = sys.platform == "win32"


class SecretError(ValueError):
    pass


def _blob_api():
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def read(b) -> bytes:
        try:
            return ctypes.string_at(b.pbData, b.cbData)
        finally:
            kernel32.LocalFree(b.pbData)

    return ctypes, crypt32, Blob, blob, read


def dpapi_protect(secret: str) -> str:
    """Encrypt ``secret`` for the current Windows user. Returns ``dpapi:BASE64``."""
    if not DPAPI_AVAILABLE:
        raise SecretError("encrypted secrets (dpapi:) are only available on Windows")
    ctypes, crypt32, Blob, blob, read = _blob_api()
    data, _keep1 = blob(secret.encode("utf-8"))
    entropy, _keep2 = blob(_ENTROPY)
    out = Blob()
    if not crypt32.CryptProtectData(ctypes.byref(data), "netmapper", ctypes.byref(entropy),
                                    None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise SecretError(f"Windows could not encrypt the secret (error {ctypes.get_last_error()})")
    return "dpapi:" + base64.b64encode(read(out)).decode("ascii")


def dpapi_unprotect(value: str) -> str:
    """Decrypt a ``dpapi:BASE64`` value written by :func:`dpapi_protect`."""
    if not DPAPI_AVAILABLE:
        raise SecretError("this config has a secret encrypted on Windows (dpapi:); "
                          "it can only be read there")
    try:
        raw = base64.b64decode(value.removeprefix("dpapi:"), validate=True)
    except ValueError:
        raise SecretError("a dpapi: secret in the config is damaged") from None
    ctypes, crypt32, Blob, blob, read = _blob_api()
    data, _keep1 = blob(raw)
    entropy, _keep2 = blob(_ENTROPY)
    out = Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(data), None, ctypes.byref(entropy),
                                      None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise SecretError("could not decrypt a dpapi: secret. It only works for the Windows "
                          "user and PC that ran 'netmapper snmp-setup'; run it again here")
    return read(out).decode("utf-8")


def resolve_secret(value: str | None) -> str | None:
    """Turn a config value into the secret itself. Plain values are returned unchanged."""
    if not value:
        return value
    if value.startswith("env:"):
        name = value[4:]
        if name not in os.environ:
            raise SecretError(f"environment variable {name} is not set")
        return os.environ[name]
    if value.startswith("dpapi:"):
        return dpapi_unprotect(value)
    return value
