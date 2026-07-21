"""MCP tool registry & governance (§8.4 / §10) — now fully config-driven.

Every tool row: name, source_type, enabled, allowed_roles, cache_ttl, pii_level,
namespaced, description, rest_mapping, and (for config tools) kind, method,
endpoint, params(JSON), result_map(JSON), arg, builtin.

- BUILTIN tools (the 6 core) have hardcoded implementations in hcm_mcp_server.py.
- CONFIG tools are defined entirely from the Admin UI (kind + endpoint + mapping)
  and executed by a generic executor (sources.run_custom) — no code needed.

Governance: the agent is only handed tools that are enabled AND allowed for the
signed-in role. Oracle still enforces the actual data.
"""
from __future__ import annotations
import json, sqlite3

import config

ALL_ROLES = "employee,manager,hr_admin"
ROLES = ("employee", "manager", "hr_admin")
KINDS = ("builtin", "oracle_search", "oracle_child", "oracle_detail", "external")

# The 6 tools that have hardcoded implementations (never dynamically registered).
BUILTIN_NAMES = {"search_workers", "get_worker", "get_assignment",
                 "get_direct_reports", "list_by_department", "get_management_chain"}

# name, source, enabled, roles, ttl, pii, ns, desc, rest_mapping, kind, method, endpoint, params, result_map, arg, builtin
_SEED = [
    ("search_workers", "oracle_hcm", 1, ALL_ROLES, 300, "low", 0,
     "Find workers by name/email/department/title.",
     "GET .../workers?q=DisplayName LIKE '%q%'", "builtin", "GET", "", "{}", "{}", "query", 1),
    ("get_worker", "oracle_hcm", 1, ALL_ROLES, 300, "medium", 0,
     "Full worker profile (job, dept, location, manager, email, phone, status).",
     "GET .../workers + child/emails + assignment", "builtin", "GET", "", "{}", "{}", "person_id", 1),
    ("get_assignment", "oracle_hcm", 1, ALL_ROLES, 300, "medium", 0,
     "Assignment: job, department, location, manager, status.",
     "assignment child / expand=workRelationships.assignments", "builtin", "GET", "", "{}", "{}", "person_id", 1),
    ("get_direct_reports", "oracle_hcm", 1, "manager,hr_admin", 300, "medium", 0,
     "A manager's direct reports.",
     "GET .../workers/{id}/child/directReports", "builtin", "GET", "", "{}", "{}", "person_id", 1),
    ("list_by_department", "oracle_hcm", 1, ALL_ROLES, 600, "low", 0,
     "List workers in a department.",
     "department-scoped query", "builtin", "GET", "", "{}", "{}", "query", 1),
    ("get_management_chain", "oracle_hcm", 1, ALL_ROLES, 600, "low", 0,
     "Reporting line upward, immediate manager first.",
     "walk manager links", "builtin", "GET", "", "{}", "{}", "person_id", 1),
    # --- CONFIG tool examples (defined by config, run by the generic executor) ---
    ("get_compensation", "oracle_hcm", 0, "hr_admin", 120, "high", 0,
     "Compensation / salary (HR only). Example CONFIG tool — child resource 'salaries'.",
     "GET .../workers/{id}/child/salaries", "oracle_child", "GET", "salaries",
     '{"onlyData":"true","limit":5}', '{"Salary":"SalaryAmount","Currency":"CurrencyCode","Basis":"SalaryBasisName"}',
     "person_id", 0),
    ("snow_raise_case", "servicenow", 0, ALL_ROLES, 0, "low", 1,
     "Raise an HR case in ServiceNow (namespaced external tool). Needs a ServiceNow source.",
     "POST /api/now/table/hr_case", "external", "POST", "hr_case", "{}", "{}", "query", 0),
]

_LIST_KINDS = {"oracle_search", "oracle_child"}


def _conn():
    c = config.tune(sqlite3.connect(config.DB_PATH, timeout=5))
    c.execute("""CREATE TABLE IF NOT EXISTS tools
                 (name TEXT PRIMARY KEY, source_type TEXT, enabled INTEGER, allowed_roles TEXT,
                  cache_ttl INTEGER, pii_level TEXT, namespaced INTEGER, description TEXT,
                  rest_mapping TEXT, kind TEXT, method TEXT, endpoint TEXT, params TEXT,
                  result_map TEXT, arg TEXT, builtin INTEGER)""")
    # migrate older DBs that predate the config columns
    for col, default in (("kind", "'builtin'"), ("method", "'GET'"), ("endpoint", "''"),
                         ("params", "'{}'"), ("result_map", "'{}'"), ("arg", "'person_id'"),
                         ("builtin", "1")):
        try:
            c.execute(f"ALTER TABLE tools ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    return c


def _seed(c):
    if c.execute("SELECT COUNT(*) FROM tools").fetchone()[0]:
        return
    c.executemany("INSERT INTO tools(name,source_type,enabled,allowed_roles,cache_ttl,pii_level,"
                  "namespaced,description,rest_mapping,kind,method,endpoint,params,result_map,arg,builtin) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _SEED)
    c.commit()


def _jload(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


def _truthy(v) -> bool:
    # robust for both INTEGER (fresh DB) and TEXT-affinity (migrated DB) columns —
    # bool("0") is True in Python, so coerce string "0"/"" to False explicitly.
    return str(v).strip().lower() not in ("0", "", "false", "none", "0.0")


def _row(r) -> dict:
    return {"name": r[0], "source_type": r[1], "enabled": _truthy(r[2]),
            "allowed_roles": [x for x in (r[3] or "").split(",") if x],
            "cache_ttl": r[4], "pii_level": r[5], "namespaced": _truthy(r[6]),
            "description": r[7], "rest_mapping": r[8], "kind": r[9] or "builtin",
            "method": r[10] or "GET", "endpoint": r[11] or "", "params": _jload(r[12], {}),
            "result_map": _jload(r[13], {}), "arg": r[14] or "person_id", "builtin": _truthy(r[15])}


_COLS = ("name,source_type,enabled,allowed_roles,cache_ttl,pii_level,namespaced,description,"
         "rest_mapping,kind,method,endpoint,params,result_map,arg,builtin")


def list_all() -> list:
    c = _conn(); _seed(c)
    rows = c.execute(f"SELECT {_COLS} FROM tools ORDER BY builtin DESC, namespaced, name").fetchall()
    c.close()
    return [_row(r) for r in rows]


def get(name: str) -> dict | None:
    c = _conn(); _seed(c)
    r = c.execute(f"SELECT {_COLS} FROM tools WHERE name=?", (name,)).fetchone()
    c.close()
    return _row(r) if r else None


def custom_enabled() -> list:
    """Enabled, non-builtin tools that should be dynamically registered with MCP."""
    return [t for t in list_all() if t["enabled"] and not t["builtin"] and t["kind"] != "external"]


def upsert(d: dict) -> dict:
    """Create or update a tool. Builtin flag/kind are protected for builtin names."""
    name = (d.get("name") or "").strip()
    if not name:
        raise ValueError("tool name required")
    existing = get(name)
    builtin = 1 if (name in BUILTIN_NAMES or (existing and existing["builtin"])) else 0
    roles = d.get("allowed_roles")
    if isinstance(roles, (list, tuple)):
        roles = ",".join(r for r in roles if r in ROLES)
    elif roles is None:
        roles = ",".join(existing["allowed_roles"]) if existing else ALL_ROLES
    kind = "builtin" if builtin else (d.get("kind") or (existing["kind"] if existing else "oracle_child"))
    params = d.get("params"); params = json.dumps(params) if isinstance(params, (dict, list)) else (params or "{}")
    rmap = d.get("result_map"); rmap = json.dumps(rmap) if isinstance(rmap, (dict, list)) else (rmap or "{}")
    row = {
        "name": name,
        "source_type": d.get("source_type", existing["source_type"] if existing else "oracle_hcm"),
        "enabled": int(d.get("enabled", existing["enabled"] if existing else 1)),
        "allowed_roles": roles,
        "cache_ttl": int(d.get("cache_ttl", existing["cache_ttl"] if existing else 300)),
        "pii_level": d.get("pii_level", existing["pii_level"] if existing else "low"),
        "namespaced": int(d.get("namespaced", existing["namespaced"] if existing else 0)),
        "description": d.get("description", existing["description"] if existing else ""),
        "rest_mapping": d.get("rest_mapping", existing["rest_mapping"] if existing else ""),
        "kind": kind, "method": d.get("method", existing["method"] if existing else "GET"),
        "endpoint": d.get("endpoint", existing["endpoint"] if existing else ""),
        "params": params, "result_map": rmap,
        "arg": d.get("arg", existing["arg"] if existing else "person_id"),
        "builtin": builtin,
    }
    c = _conn()
    c.execute(f"INSERT INTO tools({_COLS}) VALUES({','.join('?'*16)}) "
              "ON CONFLICT(name) DO UPDATE SET source_type=excluded.source_type,enabled=excluded.enabled,"
              "allowed_roles=excluded.allowed_roles,cache_ttl=excluded.cache_ttl,pii_level=excluded.pii_level,"
              "namespaced=excluded.namespaced,description=excluded.description,rest_mapping=excluded.rest_mapping,"
              "kind=excluded.kind,method=excluded.method,endpoint=excluded.endpoint,params=excluded.params,"
              "result_map=excluded.result_map,arg=excluded.arg,builtin=excluded.builtin",
              tuple(row[k] for k in ("name", "source_type", "enabled", "allowed_roles", "cache_ttl",
                                     "pii_level", "namespaced", "description", "rest_mapping", "kind",
                                     "method", "endpoint", "params", "result_map", "arg", "builtin")))
    c.commit(); c.close()
    return get(name)


def delete(name: str) -> bool:
    if name in BUILTIN_NAMES:
        return False  # never delete a built-in
    c = _conn(); cur = c.execute("DELETE FROM tools WHERE name=? AND builtin=0", (name,))
    n = cur.rowcount
    c.commit(); c.close()
    return n > 0


def set_enabled(name: str, enabled: bool):
    c = _conn(); c.execute("UPDATE tools SET enabled=? WHERE name=?", (1 if enabled else 0, name))
    c.commit(); c.close()


def set_allowed(name: str, roles: list):
    roles = ",".join([r for r in roles if r in ROLES])
    c = _conn(); c.execute("UPDATE tools SET allowed_roles=? WHERE name=?", (roles, name))
    c.commit(); c.close()


def set_ttl(name: str, ttl: int):
    c = _conn(); c.execute("UPDATE tools SET cache_ttl=? WHERE name=?", (int(ttl), name))
    c.commit(); c.close()


def allowed_names_for_role(role: str) -> set:
    return {t["name"] for t in list_all() if t["enabled"] and role in t["allowed_roles"]}


def check(name: str, role: str) -> tuple[bool, str]:
    t = get(name)
    if not t:
        return False, "unknown_tool"   # fail closed: never run a tool absent from the registry
    if not t["enabled"]:
        return False, "tool_disabled"
    if role and role not in t["allowed_roles"]:
        return False, "role_not_allowed"
    return True, "ok"


def deny_value(name: str):
    t = get(name)
    if (t and t["kind"] in _LIST_KINDS) or name in ("search_workers", "get_direct_reports",
                                                    "list_by_department", "get_management_chain"):
        return []
    return {"error": "not_permitted",
            "message": "This action isn't available for your role or is disabled."}
