"""Per-request session context: the signed-in user and their Oracle auth override.

Kept dependency-free (imports nothing from this app) so both identity.py and
sources.py can use it without an import cycle.

The *auth override* is the heart of the security model: each user signs in with
their OWN Oracle HCM account, so every REST call runs as that user and Oracle HCM
enforces data security for us. We do NOT re-implement access control — we only
read back what Oracle allows and infer the person's role from it.

Secrets in the auth override live only in memory for the length of the session.
They are never written to SQLite, never logged, and never shown to the model.
"""
from __future__ import annotations
import contextvars

_user: contextvars.ContextVar[dict] = contextvars.ContextVar("current_user", default={})
_auth: contextvars.ContextVar[dict] = contextvars.ContextVar("current_auth", default={})


def set_user(u: dict | None) -> None:
    _user.set(u or {})


def get_user() -> dict:
    return _user.get() or {}


def set_auth(a: dict | None) -> None:
    """a: {'auth':'basic','username':..,'password':..} or oauth fields, or {}."""
    _auth.set(a or {})


def get_auth() -> dict:
    return _auth.get() or {}


def clear() -> None:
    _user.set({})
    _auth.set({})
