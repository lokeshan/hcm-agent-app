"""Connector registry — configure multiple data sources (SQLite `connectors` table).

Each connector: id, name, type (mock | oracle_hcm | …), enabled, is_primary, config(JSON).
The chat is driven by the PRIMARY enabled connector; more types can be added later.
Legacy single-connection settings are migrated into a connector automatically.
"""
from __future__ import annotations
import json, sqlite3, uuid

import config
import conn_types
import crypto


def _secret_fields() -> tuple:
    """Which config keys hold secrets. Derived from the connector-type catalogue —
    any field a type renders as a password input is masked in the UI and preserved
    when the form posts it back blank. Kept in one place so adding a connector type
    with a credential can't silently forget to mask it."""
    out = {"password", "client_secret"}   # legacy keys, masked even if no type declares them
    for meta in conn_types.TYPES.values():
        out.update(f["key"] for f in meta.get("fields", []) if f.get("type") == "password")
    return tuple(sorted(out))


SECRET_FIELDS = _secret_fields()


def _conn():
    c = config.tune(sqlite3.connect(config.DB_PATH, timeout=5))
    c.execute("""CREATE TABLE IF NOT EXISTS connectors
                 (id TEXT PRIMARY KEY, name TEXT, type TEXT, enabled INTEGER,
                  is_primary INTEGER, config TEXT)""")
    return c


def _row(r) -> dict:
    cfg = json.loads(r[5] or "{}")
    for k in SECRET_FIELDS:                      # legacy plaintext passes through
        if k in cfg:
            cfg[k] = crypto.dec(cfg[k])
    return {"id": r[0], "name": r[1], "type": r[2], "enabled": bool(r[3]),
            "is_primary": bool(r[4]), "config": cfg}


_sealed = False


def _seal(c) -> None:
    """One-time upgrade for connectors stored before secrets were encrypted."""
    global _sealed
    _sealed = True
    done = []
    for cid, raw in c.execute("SELECT id, config FROM connectors").fetchall():
        cfg = json.loads(raw or "{}")
        todo = {k: v for k, v in cfg.items() if k in SECRET_FIELDS and v and not crypto.is_sealed(v)}
        if not todo:
            continue
        cfg.update({k: crypto.enc(v) for k, v in todo.items()})
        c.execute("UPDATE connectors SET config=? WHERE id=?", (json.dumps(cfg), cid))
        done.append(cid)
    if done:
        c.commit()
        print("[connectors] encrypted at rest:", ", ".join(done))


def list_all() -> list:
    c = _conn()
    _seed(c)
    if not _sealed:
        _seal(c)
    rows = [_row(r) for r in c.execute(
        "SELECT id,name,type,enabled,is_primary,config FROM connectors ORDER BY is_primary DESC, name")]
    c.close()
    return rows


def get(cid: str):
    c = _conn()
    r = c.execute("SELECT id,name,type,enabled,is_primary,config FROM connectors WHERE id=?", (cid,)).fetchone()
    c.close()
    return _row(r) if r else None


def upsert(d: dict) -> dict:
    cid = d.get("id") or uuid.uuid4().hex[:8]
    existing = get(cid)
    cfg = dict(existing["config"]) if existing else {}
    incoming = d.get("config") or {}
    for k, v in incoming.items():
        # keep existing secret if blank/masked
        if k in SECRET_FIELDS and (v in ("", None) or set(str(v)) == {"*"}):
            continue
        cfg[k] = v
    name = d.get("name", existing["name"] if existing else "Connector")
    typ = d.get("type", existing["type"] if existing else "oracle_hcm")
    enabled = int(d.get("enabled", existing["enabled"] if existing else True))
    primary = int(d.get("is_primary", existing["is_primary"] if existing else False))
    c = _conn()
    stored = {k: (crypto.enc(v) if (k in SECRET_FIELDS and v) else v) for k, v in cfg.items()}
    c.execute("INSERT INTO connectors(id,name,type,enabled,is_primary,config) VALUES(?,?,?,?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET name=excluded.name,type=excluded.type,"
              "enabled=excluded.enabled,is_primary=excluded.is_primary,config=excluded.config",
              (cid, name, typ, enabled, primary, json.dumps(stored)))
    c.commit(); c.close()
    if primary:
        set_primary(cid)
    return get(cid)


def delete(cid: str) -> None:
    c = _conn(); c.execute("DELETE FROM connectors WHERE id=?", (cid,)); c.commit(); c.close()


def set_primary(cid: str) -> None:
    c = _conn()
    c.execute("UPDATE connectors SET is_primary=0")
    c.execute("UPDATE connectors SET is_primary=1, enabled=1 WHERE id=?", (cid,))
    c.commit(); c.close()


def enabled_list() -> list:
    return [c for c in list_all() if c["enabled"]]


def primary():
    en = enabled_list()
    for c in en:
        if c["is_primary"]:
            return c
    return en[0] if en else None


def masked_list() -> list:
    out = []
    for c in list_all():
        cc = dict(c); cfg = dict(cc["config"])
        for s in SECRET_FIELDS:
            cfg[s] = "********" if cfg.get(s) else ""
        cc["config"] = cfg
        out.append(cc)
    return out


def _seed(c) -> None:
    if c.execute("SELECT COUNT(*) FROM connectors").fetchone()[0]:
        return
    # Read legacy config FIRST — before opening a write on this connection — so the
    # nested config.load() doesn't contend with our own uncommitted transaction
    # (SQLite would otherwise lock and the settings table wouldn't persist).
    cfg = config.load()
    # always have a mock source
    c.execute("INSERT INTO connectors VALUES(?,?,?,?,?,?)",
              ("mock", "Mock (sample data)", "mock", 1, 0, "{}"))
    # migrate legacy single Oracle HCM connection, if configured
    if cfg.get("hcm_base_url"):
        oc = {"base_url": cfg.get("hcm_base_url", ""), "auth": cfg.get("hcm_auth", "basic"),
              "username": cfg.get("hcm_username", ""), "password": cfg.get("hcm_password", ""),
              "client_id": cfg.get("hcm_client_id", ""), "client_secret": cfg.get("hcm_client_secret", ""),
              "token_url": cfg.get("hcm_token_url", ""), "scope": cfg.get("hcm_scope", "")}
        primary_live = cfg.get("data_source") == "live"
        c.execute("INSERT INTO connectors VALUES(?,?,?,?,?,?)",
                  (uuid.uuid4().hex[:8], "Oracle HCM", "oracle_hcm", 1, 1 if primary_live else 0, json.dumps(oc)))
        if primary_live:
            c.execute("UPDATE connectors SET is_primary=0 WHERE id='mock'")
    else:
        c.execute("UPDATE connectors SET is_primary=1 WHERE id='mock'")
    c.commit()
