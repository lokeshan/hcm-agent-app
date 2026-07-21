"""Roles & access policy (§7 A5 / §6 / §10) — config over code.

Stores two editable JSON docs:
  - capabilities : the §6 experience matrix (which role gets which capability) —
                   drives feature/nav visibility in the end-user app.
  - field_policy : declarative field rules (e.g. compensation -> hr) applied as a
                   light UX filter. NOTE: Oracle HCM is the real security boundary;
                   this only tidies what the UI shows, it does not grant access.

Role precedence: hr_admin > manager > employee.
"""
from __future__ import annotations
import json, sqlite3

import config

ROLES = ("employee", "manager", "hr_admin")

# §6 experience matrix — value is the highest role that unlocks the capability
# ("all" = everyone). Used to gate nav/features from config, not hard-coded.
DEFAULT_CAPABILITIES = {
    "own_profile": "all",
    "company_directory": "all",
    "reporting_chain": "all",
    "full_employee_record": "hr_admin",
    "my_team": "manager",
    "approvals": "manager",
    "department_listing": "manager",
    "reports_analytics": "hr_admin",
    "sensitive_fields": "hr_admin",
}

# §7 A5 field-level rules — which roles may see a sensitive field in the UI
DEFAULT_FIELD_POLICY = {
    "compensation": ["hr_admin"],
    "home_phone": ["hr_admin"],          # "self+hr" — self always sees own record
    "national_id": ["hr_admin"],
    "date_of_birth": ["hr_admin"],
}

_ORDER = {"employee": 1, "manager": 2, "hr_admin": 3}


def _conn():
    c = config.tune(sqlite3.connect(config.DB_PATH, timeout=5))
    c.execute("CREATE TABLE IF NOT EXISTS role_rules (key TEXT PRIMARY KEY, value TEXT)")
    return c


def _get(key, default):
    c = _conn()
    r = c.execute("SELECT value FROM role_rules WHERE key=?", (key,)).fetchone()
    c.close()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except Exception:
        return default


def _set(key, value):
    c = _conn()
    c.execute("INSERT INTO role_rules(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    c.commit(); c.close()


def capabilities() -> dict:
    return {**DEFAULT_CAPABILITIES, **(_get("capabilities", {}) or {})}


def field_policy() -> dict:
    return {**DEFAULT_FIELD_POLICY, **(_get("field_policy", {}) or {})}


def save_capabilities(d: dict):
    _set("capabilities", d)


def save_field_policy(d: dict):
    _set("field_policy", d)


def role_at_least(role: str, minimum: str) -> bool:
    return _ORDER.get(role, 1) >= _ORDER.get(minimum, 1)


def can(role: str, capability: str) -> bool:
    """Does this role unlock the capability, per the (editable) matrix?"""
    req = capabilities().get(capability, "all")
    if req == "all":
        return True
    return role_at_least(role, req)


def visible_field(role: str, field: str, is_self: bool = False) -> bool:
    pol = field_policy()
    if field not in pol:
        return True
    if is_self:
        return True  # you can always see your own record
    return role in pol[field]
