"""Plan-conformance audit: verify every concrete deliverable in
HR_Assistant_Redesign_Plan.md exists in the code (routes, tables, tools, types,
capabilities, settings). Run:  python tests/conformance_audit.py"""
import sys, os, re, sqlite3, tempfile
from pathlib import Path

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import config
config.DB_PATH = Path(tempfile.gettempdir()) / "hcm_conformance.db"
if config.DB_PATH.exists():
    config.DB_PATH.unlink()
import connectors, tools_registry as reg, conn_types, role_rules, users_store, audit_store, server

connectors.list_all(); reg.list_all(); role_rules.capabilities(); role_rules.field_policy()
users_store.list_all(); audit_store.list_recent(1); config.load()

def norm(p): return re.sub(r"\{[^}]+\}", "{}", p)
ROUTES = {}
for r in server.app.routes:
    if hasattr(r, "path"):
        ROUTES.setdefault(norm(r.path), set()).update(getattr(r, "methods", []) or [])

P = F = 0; missing = []; CHECKED = set()
def check(label, cond):
    global P, F
    if cond: P += 1
    else: F += 1; missing.append(label)
    print(f"  {'OK  ' if cond else 'MISS'} {label}")
def has(path, method="GET"):
    CHECKED.add(norm(path))
    return method in ROUTES.get(norm(path), set())

print("\n### §4.1 End-user pages (E1-E9)")
for u in ["/login","/","/chat","/me","/team","/directory","/reports","/approvals","/policies"]:
    check(f"GET {u}", has(u))
print("\n### §4.2 Admin pages (A0-A9)")
for u in ["/admin/login","/admin","/admin/sources","/admin/sources/new","/admin/sources/{id}",
          "/admin/tools","/admin/tools/{id}","/admin/models","/admin/roles","/admin/users",
          "/admin/cache","/admin/audit","/admin/settings"]:
    check(f"GET {u}", has(u))
print("\n### §7 Admin write actions")
for u, m in [("/admin/sources/save","POST"),("/admin/sources/{id}/test","POST"),
    ("/admin/sources/{id}/primary","POST"),("/admin/sources/{id}/delete","POST"),
    ("/admin/tools/new","GET"),("/admin/tools/save","POST"),("/admin/tools/{id}/toggle","POST"),
    ("/admin/tools/{id}/delete","POST"),("/admin/tools/{id}/test","POST"),
    ("/admin/sources/{id}/discover","POST"),("/admin/models","POST"),
    ("/admin/roles","POST"),("/admin/roles/caps","POST"),("/admin/roles/fields","POST"),
    ("/admin/users/override","POST"),("/admin/users/revoke","POST"),
    ("/admin/cache/save","POST"),("/admin/cache/clear","POST"),("/admin/audit/export","GET"),
    ("/admin/settings","POST")]:
    check(f"{m} {u}", has(u, m))
print("\n### §11 JSON APIs")
for u in ["/api/session","/api/chat","/api/me","/api/team","/api/directory","/api/reports",
          "/api/admin/sources","/api/admin/tools","/api/admin/models","/api/admin/roles",
          "/api/admin/users","/api/admin/cache","/api/admin/audit"]:
    check(f"{'POST' if u=='/api/chat' else 'GET'} {u}", has(u, "POST" if u == "/api/chat" else "GET"))
print("\n### §10 Data-model tables")
con = sqlite3.connect(config.DB_PATH)
tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ["settings","connectors","tools","users","role_rules","audit"]:
    check(f"table {t}", t in tabs)
print("\n### §8 MCP tools + governance")
names = {t["name"] for t in reg.list_all()}
for t in ["search_workers","get_worker","get_assignment","get_direct_reports","list_by_department",
          "get_management_chain","get_compensation","snow_raise_case"]:
    check(f"tool {t}", t in names)
check("get_direct_reports manager/hr only", set(reg.get("get_direct_reports")["allowed_roles"])=={"manager","hr_admin"})
check("namespaced external tool", any(t["namespaced"] for t in reg.list_all()))
print("\n### §8.3 Connector types")
types = {t["type"] for t in conn_types.list_types()}
for t in ["mock","oracle_hcm","servicenow","knowledge_base","external_mcp"]:
    check(f"type {t}", t in types)
print("\n### §6 capabilities + field policy")
for c in ["own_profile","my_team","full_employee_record","reports_analytics","sensitive_fields"]:
    check(f"capability {c}", c in role_rules.capabilities())
check("field policy compensation", "compensation" in role_rules.field_policy())
print("\n### §9 security knobs")
cfg = config.load()
for k in ["per_user_auth","hr_visibility_threshold","platform_admin_emails","hr_admin_emails",
          "admin_password_hash","bind_host","cookie_secure"]:
    check(f"setting {k}", k in cfg)

print("\n### ops endpoints")
for u in ["/healthz","/readyz","/logout","/admin/logout"]:
    check(f"GET {u}", has(u))

print("\n### reverse drift — every route is accounted for")
_FRAMEWORK = {"/openapi.json","/docs","/docs/oauth2-redirect","/redoc"}
all_routes = {norm(r.path) for r in server.app.routes if hasattr(r, "path")}
unknown = all_routes - CHECKED - _FRAMEWORK
check("no undocumented routes", not unknown, )
if unknown:
    print("   UNDOCUMENTED:", sorted(unknown))

print(f"\n===== conformance: {P} present, {F} missing =====")
if missing: print("MISSING:", missing)
sys.exit(1 if F else 0)
