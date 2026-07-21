"""Users & sessions (§7 A6 / §10) — resolved identities the app has seen.

users(email PK, display_name, person_id, role, role_override, last_seen, revoked)

`role` is what Oracle-derived detection inferred; `role_override` lets a Platform
Admin pin a role for UX (does NOT widen Oracle data access — Oracle still decides).
`revoked` forces the user to sign in again.
"""
from __future__ import annotations
import sqlite3, time

import config

ROLES = ("employee", "manager", "hr_admin")


def _conn():
    c = config.tune(sqlite3.connect(config.DB_PATH, timeout=5))
    c.execute("""CREATE TABLE IF NOT EXISTS users
                 (email TEXT PRIMARY KEY, display_name TEXT, person_id TEXT, role TEXT,
                  role_override TEXT, last_seen REAL, revoked INTEGER DEFAULT 0)""")
    return c


def _key(email: str) -> str:
    return (email or "").strip().lower()


def upsert(email: str, display_name: str, person_id, role: str) -> None:
    k = _key(email)
    if not k:
        return
    c = _conn()
    c.execute("INSERT INTO users(email,display_name,person_id,role,role_override,last_seen,revoked) "
              "VALUES(?,?,?,?,COALESCE((SELECT role_override FROM users WHERE email=?),''),?,0) "
              "ON CONFLICT(email) DO UPDATE SET display_name=excluded.display_name,"
              "person_id=excluded.person_id,role=excluded.role,last_seen=excluded.last_seen,revoked=0",
              (k, display_name, str(person_id or ""), role, k, time.time()))
    c.commit(); c.close()


def set_override(email: str, role: str) -> None:
    role = role if role in ROLES else ""
    c = _conn(); c.execute("UPDATE users SET role_override=? WHERE email=?", (role, _key(email)))
    c.commit(); c.close()


def revoke(email: str) -> None:
    c = _conn(); c.execute("UPDATE users SET revoked=1 WHERE email=?", (_key(email),))
    c.commit(); c.close()


def is_revoked(email: str) -> bool:
    c = _conn()
    r = c.execute("SELECT revoked FROM users WHERE email=?", (_key(email),)).fetchone()
    c.close()
    return bool(r and r[0])


def get_override(email: str) -> str:
    c = _conn()
    r = c.execute("SELECT role_override FROM users WHERE email=?", (_key(email),)).fetchone()
    c.close()
    return (r[0] if r else "") or ""


def list_all() -> list:
    c = _conn()
    rows = c.execute("SELECT email,display_name,person_id,role,role_override,last_seen,revoked "
                     "FROM users ORDER BY last_seen DESC").fetchall()
    c.close()
    out = []
    for r in rows:
        out.append({"email": r[0], "display_name": r[1], "person_id": r[2], "role": r[3],
                    "role_override": r[4] or "", "effective_role": (r[4] or r[3] or "employee"),
                    "last_seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[5])) if r[5] else "",
                    "revoked": bool(r[6])})
    return out
