"""Identify the signed-in user and DERIVE their role from what Oracle returns.

Security model: each user connects to Oracle HCM with their OWN account, so Oracle
enforces data access. We don't re-implement permissions — we infer the role from
the breadth of data the account can see:

  - Employee  -> can see (essentially) only their own worker record.
  - Manager   -> has at least one direct report (reportees' data comes back).
  - HR Admin  -> can see SECURED data for people beyond their own reports.

`hr_admin_emails` in config is kept as an explicit override / fallback for cases
where the automatic probe can't tell (e.g. an HR service login, or a pod whose
data-security config needs manual calibration).

A per-request "current user" + "current auth" context (session_ctx) lets the MCP
tools and the offline brain resolve "me / my / my team" and run REST calls as the
signed-in user.
"""
from __future__ import annotations

import config
import sources
import session_ctx

# --- current-user context (thin wrappers over session_ctx for the app/tools) ---


def set_current(user: dict | None) -> None:
    session_ctx.set_user(user or {})


def get_current() -> dict:
    return session_ctx.get_user()


def set_current_auth(auth: dict | None) -> None:
    session_ctx.set_auth(auth or {})


def get_current_auth() -> dict:
    return session_ctx.get_auth()


# --- role logic ---------------------------------------------------------------


def _hr_list() -> set:
    raw = (config.load().get("hr_admin_emails", "") or "")
    return {x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip()}


def _classify(email: str, pnum, il: str, signals: dict) -> str:
    """Employee < Manager < HR Admin, from explicit override then live signals."""
    hr = _hr_list()
    if (email and email.lower() in hr) or (pnum and str(pnum) in hr) or (il in hr):
        return "hr_admin"
    # data-driven: can this account see secured data beyond its own reports?
    if signals.get("secured_beyond_reports") or signals.get("hr_hint"):
        return "hr_admin"
    if (signals.get("report_count") or 0) > 0:
        return "manager"
    return "employee"


async def _search(q):
    return await sources.search(q)


async def whoami(identifier: str, password: str | None = None) -> dict | None:
    """Resolve a sign-in identifier (work email, name, or employee #) to identity + role.

    If `password` is supplied, the user's own Oracle credentials are used for the
    lookup + role probe (per-user auth), and returned under `_auth` so the app can
    reuse them for every subsequent query in the session.
    """
    ident = (identifier or "").strip()
    if not ident:
        return None
    il = ident.lower()

    # per-user auth override — the probe below then runs AS this user.
    auth: dict = {}
    if password:
        auth = {"auth": "basic", "username": ident, "password": password}
        session_ctx.set_auth(auth)

    results = await _search(ident)
    person = None
    for r in (results or []):
        if str(r.get("WorkEmail", "")).lower() == il or str(r.get("PersonNumber", "")) == ident:
            person = r
            break
    person = person or (results[0] if results else None)

    if not person:
        # unknown to the directory — still honour the HR allow-list (e.g. an HR service login)
        if il in _hr_list():
            return {"person_id": None, "person_number": None, "display_name": ident,
                    "email": ident if "@" in ident else "", "role": "hr_admin",
                    "report_count": 0, "resolved": False, "_auth": auth}
        return None

    pid = person.get("PersonId") or person.get("PersonNumber")
    pnum = person.get("PersonNumber")
    name = person.get("DisplayName")
    email = person.get("WorkEmail") or (ident if "@" in ident else "")

    signals = await sources.role_signals(pid)
    role = _classify(email, pnum, il, signals)

    return {"person_id": pid, "person_number": pnum, "display_name": name, "email": email,
            "role": role, "report_count": int(signals.get("report_count") or 0),
            "signals": signals, "resolved": True, "_auth": auth}
