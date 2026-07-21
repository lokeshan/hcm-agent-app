"""Encrypt secrets at rest (Oracle passwords, API keys, MCP tokens).

Values are stored as ``enc:<fernet token>``. Anything without that prefix is treated
as legacy plaintext and returned unchanged, so an existing database keeps working and
seals itself on first read — no manual migration step.

The key comes from ``HCM_SECRET_KEY`` or, failing that, a generated ``.secret_key``
file beside the database.

What this does and does not buy you: a local key file protects a database that gets
copied, backed up, or emailed around — the common way these credentials actually
escape. It is NOT protection from someone who already has the whole directory, since
the key is sitting next to the data. For that, set HCM_SECRET_KEY from a secret
manager and keep it off the filesystem entirely.
"""
from __future__ import annotations
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:"
_KEY_FILE = Path(os.getenv("HCM_SECRET_KEY_FILE") or (Path(__file__).resolve().parent / ".secret_key"))
_fernet_cached = None


def _load_or_create_key() -> str:
    if _KEY_FILE.exists():
        k = _KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    key = Fernet.generate_key().decode()
    _KEY_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(_KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)   # owner-only where the FS supports it
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    global _fernet_cached
    if _fernet_cached is None:
        _fernet_cached = Fernet((os.getenv("HCM_SECRET_KEY") or "").strip() or _load_or_create_key())
    return _fernet_cached


def enc(v):
    """Encrypt a value for storage. Already-encrypted and empty values pass through."""
    if v in ("", None) or (isinstance(v, str) and v.startswith(PREFIX)):
        return v
    return PREFIX + _fernet().encrypt(str(v).encode()).decode()


def dec(v):
    """Decrypt a stored value. Legacy plaintext passes through unchanged."""
    if not isinstance(v, str) or not v.startswith(PREFIX):
        return v
    try:
        return _fernet().decrypt(v[len(PREFIX):].encode()).decode()
    except InvalidToken:
        # wrong or rotated key — hand back nothing rather than a useless ciphertext
        # that would be sent to Oracle as a password and lock the account out.
        return ""


def is_sealed(v) -> bool:
    return isinstance(v, str) and v.startswith(PREFIX)


if __name__ == "__main__":   # self-check: round-trip, passthrough, tamper
    assert dec(enc("hunter2")) == "hunter2"
    assert enc("") == "" and dec("") == ""
    assert dec("plaintext-legacy") == "plaintext-legacy"
    once = enc("x")
    assert enc(once) == once                       # idempotent: never double-wrapped
    assert is_sealed(enc("x")) and not is_sealed("x")
    assert dec(PREFIX + "garbage") == ""
    print("crypto self-check OK")
