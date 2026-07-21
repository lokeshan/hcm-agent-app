"""End-to-end test of the whole platform in-process (ASGI, mock data, offline demo brain).
Covers all routes, role nav, admin gate, governance, audit, CRUD, JSON APIs, and the
security hardening (CSRF tokens, admin password, guest-on-live block).
Run:  python tests/test_e2e.py"""
import asyncio, sys, os, re, tempfile
from pathlib import Path

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import config
config.DB_PATH = Path(tempfile.gettempdir()) / "hcm_e2e.db"
if config.DB_PATH.exists():
    config.DB_PATH.unlink()
import connectors
connectors.list_all(); connectors.set_primary("mock")

import httpx
from httpx import ASGITransport
import server

# configure admin auth deterministically (password + allow-list)
_SALT = "testsalt"
config.save({"ai_provider": "demo", "ai_api_key": "", "platform_admin_emails": "admin@acme.com",
             "admin_salt": _SALT, "admin_password_hash": server._pwhash("secret", _SALT)})

P = 0; F = 0
def ok(label, cond, extra=""):
    global P, F
    if cond: P += 1; print(f"  PASS {label}")
    else: F += 1; print(f"  FAIL {label}  {extra}")
def client(): return httpx.AsyncClient(transport=ASGITransport(app=server.app), base_url="http://t")
async def login(c, ident, password=None):
    d = {"identifier": ident}
    if password: d["password"] = password
    return await c.post("/login", data=d, follow_redirects=True)
async def _csrf(c, path):
    r = await c.get(path)
    m = re.search(r'name="_csrf" value="([^"]+)"', r.text) or re.search(r'id="csrf" value="([^"]+)"', r.text)
    return m.group(1) if m else ""
async def chat(c, msg):
    tok = await _csrf(c, "/chat")
    return await c.post("/api/chat", json={"message": msg}, headers={"X-CSRF-Token": tok})
async def apost(c, path, data=None, **kw):
    tok = await _csrf(c, "/admin/tools")
    d = dict(data or {}); d["_csrf"] = tok
    return await c.post(path, data=d, **kw)

async def main():
    print("\n== Employee (Jane Doe) ==")
    emp = client()
    r = await emp.get("/login"); ok("/login renders", r.status_code == 200 and "Sign in" in r.text)
    r = await login(emp, "jane.doe@example.com")
    ok("employee -> home", r.status_code == 200 and "Good day" in r.text)
    ok("nav has NO My Team", 'href="/team"' not in r.text)
    ok("nav has NO Reports", 'href="/reports"' not in r.text)
    r = await emp.get("/me"); ok("/me shows Jane Doe", "Jane Doe" in r.text)
    r = await emp.get("/directory?q=Chen"); ok("/directory finds Michael Chen", "Michael Chen" in r.text)
    r = await chat(emp, "who is my manager?"); j = r.json()
    ok("chat: my manager -> Michael Chen", "Michael Chen" in j.get("answer", ""), j)
    ok("chat used get_assignment", "get_assignment" in j.get("tools", []))
    r = await emp.get("/admin", follow_redirects=False)
    ok("employee blocked from /admin", r.status_code == 303 and "/admin/login" in r.headers.get("location", ""))
    r = await chat(emp, "who reports to Priya Nair?")
    ok("employee direct-reports governed (no crash)", r.status_code == 200)

    print("\n== Security controls ==")
    r = await emp.post("/api/chat", json={"message": "hi"})   # no CSRF header
    ok("chat without CSRF is rejected (403)", r.status_code == 403, r.status_code)
    r = await emp.post("/admin/tools/get_worker/toggle", follow_redirects=False)  # no token + not admin
    ok("admin POST without CSRF is rejected (403)", r.status_code == 403, r.status_code)
    r = await chat(emp, "x" * 5000)
    ok("oversized chat message rejected (413)", r.status_code == 413, r.status_code)

    print("\n== Manager (Michael Chen) ==")
    mgr = client(); r = await login(mgr, "michael.chen@example.com")
    ok("manager signs in", "Manager" in r.text); ok("nav HAS My Team", 'href="/team"' in r.text)
    r = await mgr.get("/team"); ok("/team lists Jane Doe", "Jane Doe" in r.text)
    r = await chat(mgr, "who is on my team?"); j = r.json()
    ok("chat: my team -> Jane Doe", "Jane Doe" in j.get("answer", ""), j)
    ok("chat used get_direct_reports", "get_direct_reports" in j.get("tools", []))

    print("\n== HR Admin (Sara Williams) ==")
    hr = client(); r = await login(hr, "sara.williams@example.com")
    ok("HR signs in", "HR Admin" in r.text); ok("nav HAS Reports", 'href="/reports"' in r.text)
    r = await hr.get("/reports"); ok("/reports headcount", "Headcount by department" in r.text and "Engineering" in r.text)

    print("\n== Admin Console (password + allow-list) ==")
    adm = client()
    r = await adm.post("/admin/login", data={"identifier": "admin@acme.com", "password": "wrong"},
                       follow_redirects=False)
    ok("wrong admin password rejected (403)", r.status_code == 403, r.status_code)
    r = await adm.post("/admin/login", data={"identifier": "nope@x.com", "password": "secret"},
                       follow_redirects=False)
    ok("non-allowlisted admin rejected (403)", r.status_code == 403, r.status_code)
    r = await adm.post("/admin/login", data={"identifier": "admin@acme.com", "password": "secret"},
                       follow_redirects=True)
    ok("correct admin creds -> dashboard", "Dashboard" in r.text)
    for path, needle in [("/admin/sources", "Oracle HCM"), ("/admin/sources/new?type=oracle_hcm", "Base URL"),
                         ("/admin/tools", "get_direct_reports"), ("/admin/tools/get_worker", "Save tool"),
                         ("/admin/models", "Provider"), ("/admin/roles", "Capability matrix"),
                         ("/admin/users", "jane.doe@example.com"), ("/admin/cache", "Hit-rate"),
                         ("/admin/audit", "denied"), ("/admin/settings", "Admin password")]:
        r = await adm.get(path); ok(f"admin page {path}", needle in r.text)
    r = await adm.get("/admin/audit/export"); ok("audit CSV export", r.text.startswith("when,user,role"))

    print("\n== Admin writes (CSRF-protected) ==")
    await apost(adm, "/admin/tools/get_compensation/toggle", follow_redirects=True)
    comp = next((t for t in __import__("tools_registry").list_all() if t["name"] == "get_compensation"), None)
    ok("enable get_compensation persists", comp and comp["enabled"] is True)
    r = await apost(adm, "/admin/sources/save", data={"type": "servicenow", "name": "ServiceNow Dev",
        "cfg_base_url": "https://dev.service-now.com", "enabled": "on"}, follow_redirects=True)
    ok("add ServiceNow source", "ServiceNow Dev" in r.text)
    await apost(adm, "/admin/users/override", data={"email": "jane.doe@example.com", "role": "manager"},
                follow_redirects=True)
    r = await emp.get("/"); ok("role override flips Jane to Manager", 'href="/team"' in r.text)

    print("\n== JSON APIs ==")
    r = await mgr.get("/api/session"); ok("/api/session role", r.json()["identity"]["role"] == "manager")
    r = await mgr.get("/api/team"); ok("/api/team reports", len(r.json().get("reports", [])) >= 1)
    r = await hr.get("/api/reports"); ok("/api/reports overview", r.json().get("total", 0) > 0)
    r = await emp.get("/api/admin/tools"); ok("/api/admin/tools blocked for non-admin", r.status_code == 403)

    print("\n== Configurable tools (add / run / delete, no code) ==")
    import tools_registry as _reg, sources as _src, hcm_mcp_server as _srv, role_rules as _rr
    r = await adm.get("/admin/tools/new"); ok("new-tool form renders", "New MCP Tool" in r.text and "Endpoint" in r.text)
    r = await apost(adm, "/admin/tools/save", data={
        "name": "list_absences", "description": "List a worker's absences", "source_type": "oracle_hcm",
        "kind": "oracle_child", "arg": "person_id", "endpoint": "absences", "params": '{"limit":10}',
        "result_map": "{}", "cache_ttl": "60", "pii_level": "medium",
        "role_employee": "on", "role_manager": "on", "role_hr_admin": "on", "enabled": "on"}, follow_redirects=True)
    ok("new tool shows in registry list", "list_absences" in r.text and "oracle_child" in r.text)
    ok("registry persisted new tool", _reg.get("list_absences") is not None)
    ok("not builtin (config tool)", _reg.get("list_absences")["builtin"] is False)
    ok("dynamically registered on MCP server", "list_absences" in _srv._registered)
    rows = await _src.run_custom("list_absences", person_id="100001")
    ok("UI-added tool executes (mock)", isinstance(rows, list) and len(rows) >= 1, rows)
    r = await apost(adm, "/admin/tools/save", data={"name": "bad", "kind": "oracle_child",
        "params": "{oops", "enabled": "on", "role_employee": "on"}, follow_redirects=False)
    ok("invalid JSON rejected", r.status_code == 303 and "err=" in r.headers.get("location", ""))
    r = await apost(adm, "/admin/tools/list_absences/delete", follow_redirects=True)
    ok("tool deleted from list", "list_absences" not in r.text)
    ok("deregistered from MCP", "list_absences" not in _srv._registered)
    ok("built-in tool cannot be deleted", _reg.delete("get_worker") is False and _reg.get("get_worker") is not None)

    print("\n== Editable roles & field policy ==")
    await apost(adm, "/admin/roles/caps", data={"cap_my_team": "hr_admin"}, follow_redirects=True)
    ok("capability edit persisted", _rr.capabilities().get("my_team") == "hr_admin")
    await apost(adm, "/admin/roles/fields", data={"fp_compensation_hr_admin": "on"}, follow_redirects=True)
    ok("field policy edit persisted", "hr_admin" in _rr.field_policy().get("compensation", []))

    print(f"\n===== {P} passed, {F} failed =====")
    sys.exit(1 if F else 0)

asyncio.run(main())
