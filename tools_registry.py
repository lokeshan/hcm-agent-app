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
KINDS = ("builtin", "oracle_search", "oracle_child", "oracle_detail",
         "oracle_resource", "external")
METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")
ARG_TYPES = ("string", "integer", "number", "boolean")

# The 6 tools that have hardcoded implementations (never dynamically registered).
# Builtins that return a LIST. A denial must hand back [] for these, not an error
# dict, or callers that iterate the result blow up on a permission check. Keep in
# step with the return annotations in hcm_mcp_server.
LIST_BUILTINS = {"search_workers", "get_direct_reports", "list_by_department",
                 "get_management_chain", "get_team_goals"}

BUILTIN_NAMES = {"search_workers", "get_worker", "get_assignment",
                 "get_direct_reports", "list_by_department", "get_management_chain",
                 "get_team_goals"}

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
    ("get_team_goals", "oracle_hcm", 1, "manager,hr_admin", 300, "medium", 0,
     "Goals for everyone reporting to a manager, grouped per person. Use for "
     "'how is my team tracking', 'show my team's goals'.",
     "direct reports, then performanceGoalsV2 per report", "builtin", "GET", "",
     "{}", "{}", "person_id", 1),
    # --- Goals & Learning (top-level Fusion resources, verified against a live pod) ---
    ("get_goals", "oracle_hcm", 1, ALL_ROLES, 300, "medium", 0,
     "A worker's performance goals: name, progress %, target date, weight, goal plan. "
     "Use for 'my goals', 'how am I tracking', 'what are Priya's goals'.",
     "GET .../performanceGoalsV2?q=PersonNumber='{n}'", "oracle_resource", "GET", "performanceGoalsV2",
     '{"attr":"PersonNumber","limit":50,'
     '"fields":"GoalName,Description,StatusMeaning,PercentCompletion,StartDate,TargetCompletionDate,'
     'Weighting,CategoryMeaning,LevelMeaning,PriorityMeaning,GoalPlanName,ReviewPeriodName"}',
     '{"Goal":"GoalName","Progress":"PercentCompletion","Target":"TargetCompletionDate",'
     '"Status":"StatusMeaning","Weight":"Weighting","Category":"CategoryMeaning","Plan":"GoalPlanName"}',
     "person_number", 0),
    ("get_development_goals", "oracle_hcm", 1, ALL_ROLES, 300, "medium", 0,
     "A worker's development goals (growth/learning objectives), with success criteria and priority.",
     "GET .../developmentGoals?q=PersonNumber='{n}'", "oracle_resource", "GET", "developmentGoals",
     '{"attr":"PersonNumber","limit":50,'
     '"fields":"GoalName,Description,StatusMeaning,PercentComplete,StartDate,TargetCompletionDate,'
     'CategoryMeaning,PriorityMeaning,LevelMeaning,SuccessCriteria"}',
     '{"Goal":"GoalName","Progress":"PercentComplete","Target":"TargetCompletionDate",'
     '"Status":"StatusMeaning","Priority":"PriorityMeaning","SuccessCriteria":"SuccessCriteria"}',
     "person_number", 0),
    ("get_learning", "oracle_hcm", 1, ALL_ROLES, 300, "medium", 0,
     "A worker's learning records: assigned courses, status, due date, completion date and score. "
     "Use for 'my training', 'what learning is overdue', 'did Sam finish the course'.",
     "GET .../learnerLearningRecords?q=assignedToNumber='{n}'", "oracle_resource", "GET",
     "learnerLearningRecords",
     '{"attr":"assignedToNumber","limit":50,'
     '"fields":"learningItemTitle,learningItemTypeMeaning,assignmentStatusMeaning,assignmentDueDate,'
     'completedDate,actualScore,assignmentTypeMeaning,assignedDate"}',
     '{"Course":"learningItemTitle","Type":"learningItemTypeMeaning","Status":"assignmentStatusMeaning",'
     '"Due":"assignmentDueDate","Completed":"completedDate","Score":"actualScore"}',
     "person_number", 0),
    ("snow_raise_case", "servicenow", 0, ALL_ROLES, 0, "low", 1,
     "Raise an HR case in ServiceNow (namespaced external tool). Needs a ServiceNow source.",
     "POST /api/now/table/hr_case", "external", "POST", "hr_case", "{}", "{}", "query", 0),
]

_LIST_KINDS = {"oracle_search", "oracle_child", "oracle_resource"}
_repaired = False


def _conn():
    c = config.tune(sqlite3.connect(config.DB_PATH, timeout=5))
    c.execute("""CREATE TABLE IF NOT EXISTS tools
                 (name TEXT PRIMARY KEY, source_type TEXT, enabled INTEGER, allowed_roles TEXT,
                  cache_ttl INTEGER, pii_level TEXT, namespaced INTEGER, description TEXT,
                  rest_mapping TEXT, kind TEXT, method TEXT, endpoint TEXT, params TEXT,
                  result_map TEXT, arg TEXT, builtin INTEGER, args_schema TEXT DEFAULT '')""")
    # migrate older DBs that predate the config columns
    for col, default in (("kind", "'builtin'"), ("method", "'GET'"), ("endpoint", "''"),
                         ("params", "'{}'"), ("result_map", "'{}'"), ("arg", "'person_id'"),
                         ("builtin", "1"), ("args_schema", "''")):
        try:
            c.execute(f"ALTER TABLE tools ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    _repair_builtin(c)
    return c


def _repair_builtin(c):
    """`ALTER TABLE ... builtin DEFAULT 1` stamps EVERY pre-existing row as builtin —
    SQLite has no conditional default — so on a database predating the config columns,
    config tools like get_compensation came out flagged builtin. That made them
    undeletable, uneditable, and invisible to custom_enabled(): enabling one in the UI
    appeared to work and did nothing, because there is no hardcoded implementation to
    fall back on. Only the six names in BUILTIN_NAMES are ever builtin; reset the rest
    and restore their real definition from the seed."""
    global _repaired
    if _repaired:
        return
    _repaired = True
    try:
        rows = c.execute("SELECT name FROM tools WHERE builtin NOT IN ('0', 0)").fetchall()
    except sqlite3.OperationalError:
        return
    wrong = [r[0] for r in rows if r[0] not in BUILTIN_NAMES]
    if not wrong:
        return
    seed = {s[0]: s for s in _SEED}
    for name in wrong:
        s = seed.get(name)
        if s:   # restore kind/method/endpoint/params/result_map/arg/namespaced/mapping
            c.execute("UPDATE tools SET builtin=0, kind=?, method=?, endpoint=?, params=?, "
                      "result_map=?, arg=?, namespaced=?, rest_mapping=? WHERE name=?",
                      (s[9], s[10], s[11], s[12], s[13], s[14], s[6], s[8], name))
        else:   # legacy config tool with no seed: un-flag it so it is at least editable
            c.execute("UPDATE tools SET builtin=0, kind='oracle_child' "
                      "WHERE name=? AND kind='builtin'", (name,))
    c.commit()
    print("[tools] repaired builtin flag on:", ", ".join(wrong))


def _seed(c):
    if c.execute("SELECT COUNT(*) FROM tools").fetchone()[0]:
        return
    c.executemany("INSERT INTO tools(name,source_type,enabled,allowed_roles,cache_ttl,pii_level,"
                  "namespaced,description,rest_mapping,kind,method,endpoint,params,result_map,arg,builtin) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _SEED)
    c.commit()


def _backfill(c):
    """_seed only fires on an empty table, so a database created before these tools
    existed would never get them. Insert any seed row whose name is missing."""
    have = {r[0] for r in c.execute("SELECT name FROM tools")}
    new = [s for s in _SEED if s[0] not in have]
    if not new:
        return
    c.executemany("INSERT INTO tools(name,source_type,enabled,allowed_roles,cache_ttl,pii_level,"
                  "namespaced,description,rest_mapping,kind,method,endpoint,params,result_map,arg,builtin) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", new)
    c.commit()
    print("[tools] added:", ", ".join(s[0] for s in new))


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
            "result_map": _jload(r[13], {}), "arg": r[14] or "person_id", "builtin": _truthy(r[15]),
            "args_schema": _jload(r[16] if len(r) > 16 else "", [])}


_COLS = ("name,source_type,enabled,allowed_roles,cache_ttl,pii_level,namespaced,description,"
         "rest_mapping,kind,method,endpoint,params,result_map,arg,builtin,args_schema")


def args_of(t: dict) -> list:
    """A tool's argument list, normalised. Falls back to the legacy single `arg`
    column so every tool defined before multi-arg keeps working untouched."""
    out = []
    for it in (t.get("args_schema") or []):
        if isinstance(it, str):
            it = {"name": it}
        nm = str((it or {}).get("name") or "").strip()
        if nm:
            out.append({"name": nm, "type": it.get("type") or "string",
                        "required": bool(it.get("required", True)),
                        "description": it.get("description") or ""})
    return out or [{"name": t.get("arg") or "person_id", "type": "string",
                    "required": True, "description": ""}]


def json_schema(t: dict) -> dict:
    """MCP inputSchema for a config tool. fastmcp normally derives this from a
    Python signature, which caps a tool at the arguments we hard-coded; building it
    here is what lets an admin declare any number of them."""
    props, req = {}, []
    for a in args_of(t):
        p = {"type": a["type"] if a["type"] in ARG_TYPES else "string"}
        if a["description"]:
            p["description"] = a["description"]
        props[a["name"]] = p
        if a["required"]:
            req.append(a["name"])
    return {"type": "object", "properties": props, "required": req,
            "additionalProperties": False}


def list_all() -> list:
    c = _conn(); _seed(c); _backfill(c)
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
    return [t for t in list_all() if t["enabled"] and not t["builtin"]]


def upsert(d: dict) -> dict:
    """Create or update a tool. Builtin flag/kind are protected for builtin names."""
    name = (d.get("name") or "").strip()
    if not name:
        raise ValueError("tool name required")
    existing = get(name)
    # BUILTIN_NAMES is the only authority. Deriving this from the stored flag instead
    # let a wrongly-migrated row stay builtin forever (see _repair_builtin).
    builtin = 1 if name in BUILTIN_NAMES else 0
    roles = d.get("allowed_roles")
    if isinstance(roles, (list, tuple)):
        roles = ",".join(r for r in roles if r in ROLES)
    elif roles is None:
        roles = ",".join(existing["allowed_roles"]) if existing else ALL_ROLES
    kind = "builtin" if builtin else (d.get("kind") or (existing["kind"] if existing else "oracle_child"))
    params = d.get("params"); params = json.dumps(params) if isinstance(params, (dict, list)) else (params or "{}")
    rmap = d.get("result_map"); rmap = json.dumps(rmap) if isinstance(rmap, (dict, list)) else (rmap or "{}")
    aschema = d.get("args_schema")
    if isinstance(aschema, (list, tuple)):
        aschema = json.dumps([a for a in aschema if a])
    elif aschema is None:
        aschema = json.dumps(existing["args_schema"]) if (existing and existing["args_schema"]) else ""
    else:
        aschema = str(aschema or "")
    method = str(d.get("method", existing["method"] if existing else "GET")).upper()
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
        "kind": kind, "method": method if method in METHODS else "GET",
        "endpoint": d.get("endpoint", existing["endpoint"] if existing else ""),
        "params": params, "result_map": rmap,
        "arg": d.get("arg", existing["arg"] if existing else "person_id"),
        "builtin": builtin, "args_schema": aschema,
    }
    c = _conn()
    c.execute(f"INSERT INTO tools({_COLS}) VALUES({','.join('?'*17)}) "
              "ON CONFLICT(name) DO UPDATE SET source_type=excluded.source_type,enabled=excluded.enabled,"
              "allowed_roles=excluded.allowed_roles,cache_ttl=excluded.cache_ttl,pii_level=excluded.pii_level,"
              "namespaced=excluded.namespaced,description=excluded.description,rest_mapping=excluded.rest_mapping,"
              "kind=excluded.kind,method=excluded.method,endpoint=excluded.endpoint,params=excluded.params,"
              "result_map=excluded.result_map,arg=excluded.arg,builtin=excluded.builtin,"
              "args_schema=excluded.args_schema",
              tuple(row[k] for k in ("name", "source_type", "enabled", "allowed_roles", "cache_ttl",
                                     "pii_level", "namespaced", "description", "rest_mapping", "kind",
                                     "method", "endpoint", "params", "result_map", "arg", "builtin",
                                     "args_schema")))
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


def write_tool_names() -> set:
    """Enabled tools that CHANGE data in a connected system (anything not a GET).
    The model can decide to call these on its own, so chat gates them behind an
    explicit user approval — governance says *may*, approval says *now*."""
    return {t["name"] for t in list_all()
            if t["enabled"] and str(t.get("method") or "GET").upper() != "GET"}


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
    if (t and t["kind"] in _LIST_KINDS) or name in LIST_BUILTINS:
        return []
    return {"error": "not_permitted",
            "message": "This action isn't available for your role or is disabled."}
