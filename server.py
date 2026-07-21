"""HR Assistant platform — ONE FastAPI backend serving TWO apps (§3, §11):

  • End-User App (HR Assistant)  —  /login /  /chat /me /team /directory /reports /approvals /policies
  • Admin Console (Platform)     —  /admin/login /admin /admin/sources /admin/tools /admin/models
                                     /admin/roles /admin/users /admin/cache /admin/audit /admin/settings

Separation is real: different URLs, different nav, different auth gates. End users
never see admin; non-admins hitting /admin/* are bounced to the admin sign-in.

Security: users connect to Oracle with their OWN account (per_user_auth) and Oracle
enforces data access. Role is inferred for UX only. Run:  python server.py
"""
from __future__ import annotations

try:
    import truststore; truststore.inject_into_ssl()
except Exception:
    pass

import secrets as _secrets
import time
import hashlib
import logging
import collections
from urllib.parse import quote, parse_qs

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse

import json
import config, connectors, conn_types, sources, identity as idmod
import tools_registry as registry
import users_store, role_rules, audit_store, cache, models as models_mod
import webui as ui
from agent import build_agent, active_mode
from hcm_mcp_server import sync_custom_tools

app = FastAPI(title="HR Assistant Platform")
COOKIE = "hcm_sid"
SESSIONS: dict[str, dict] = {}
MAX_SESSIONS = 2000        # bound the in-memory session store
MAX_HISTORY = 24           # cap stored chat turns per session
MAX_MSG_LEN = 4000         # reject oversized chat messages
_CSRF_EXEMPT = {"/login", "/admin/login"}


def _cookie_secure() -> bool:
    return config.as_bool(config.load().get("cookie_secure", "false"))


def _set_sid_cookie(resp, sid):
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax", secure=_cookie_secure())


# ----------------------------------------------------------------- sessions ---
def _sess(request: Request):
    sid = request.cookies.get(COOKIE)
    return SESSIONS.get(sid) if sid else None


def _new_session(resp) -> str:
    if len(SESSIONS) > MAX_SESSIONS:                       # evict oldest to bound memory
        for k in list(SESSIONS)[: len(SESSIONS) - MAX_SESSIONS + 1]:
            SESSIONS.pop(k, None)
    sid = _secrets.token_urlsafe(18)
    SESSIONS[sid] = {"csrf": _secrets.token_urlsafe(18)}
    _set_sid_cookie(resp, sid)
    return sid


def _ensure_sid(request: Request, resp) -> str:
    sid = request.cookies.get(COOKIE)
    if not sid or sid not in SESSIONS:
        return _new_session(resp)
    return sid


def _rotate(request: Request, resp, **data) -> str:
    """Fresh session id on login / privilege change (anti-fixation); carries data over."""
    old = request.cookies.get(COOKIE)
    if old:
        SESSIONS.pop(old, None)
    sid = _new_session(resp)
    SESSIONS[sid].update(data)
    return sid


def _session_csrf(request: Request) -> str:
    s = _sess(request)
    if s is None:
        return ""
    if not s.get("csrf"):
        s["csrf"] = _secrets.token_urlsafe(18)
    return s["csrf"]


def _client_is_local(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient") or host.startswith("127.")


def _pwhash(pw: str, salt: str) -> str:
    return hashlib.sha256((salt + (pw or "")).encode("utf-8")).hexdigest()


log = logging.getLogger("hcm")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

# --- rate limiting + auth lockout (in-memory sliding windows) ---
_RATE: dict = collections.defaultdict(list)
_LOCKOUT: dict = collections.defaultdict(list)
_RATE_RULES = {"/api/chat": 40, "/login": 15, "/admin/login": 15}


def _rate_ok(key, limit, window=60):
    now = time.time(); q = _RATE[key]
    while q and q[0] <= now - window:
        q.pop(0)
    if len(q) >= limit:
        return False
    q.append(now); return True


def _client_ip(request: Request):
    return request.client.host if request.client else "?"


def _auth_locked(ip):
    now = time.time(); q = _LOCKOUT[ip]
    while q and q[0] <= now - 300:
        q.pop(0)
    return len(q) >= 8


def _auth_fail(ip):
    _LOCKOUT[ip].append(time.time())


_SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                "connect-src 'self'; base-uri 'self'; form-action 'self'"),
}


@app.middleware("http")
async def _obs_mw(request: Request, call_next):
    rid = _secrets.token_hex(4)
    t0 = time.time(); path = request.url.path
    if request.method == "POST" and path in _RATE_RULES:
        if not _rate_ok(f"{_client_ip(request)}:{path}", _RATE_RULES[path]):
            return JSONResponse({"error": "Too many requests. Please slow down."}, status_code=429)
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("unhandled error rid=%s %s %s", rid, request.method, path)
        return JSONResponse({"error": f"Internal error (ref {rid})."}, status_code=500)
    for k, v in _SEC_HEADERS.items():
        resp.headers.setdefault(k, v)
    resp.headers["X-Request-ID"] = rid
    if path != "/healthz":
        log.info("rid=%s %s %s -> %s %.0fms", rid, request.method, path, resp.status_code, (time.time() - t0) * 1000)
    return resp


class _CSRFMiddleware:
    """Pure-ASGI CSRF guard. Buffers and replays the request body so downstream
    handlers still receive it (unlike BaseHTTPMiddleware, which consumes it)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" \
                or scope.get("path", "") in _CSRF_EXEMPT:
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = None
        for part in headers.get("cookie", "").split(";"):
            part = part.strip()
            if part.startswith(COOKIE + "="):
                token = (SESSIONS.get(part[len(COOKIE) + 1:]) or {}).get("csrf")
        if "application/json" in headers.get("content-type", ""):
            supplied = headers.get("x-csrf-token", "")
            if not token or not _secrets.compare_digest(str(supplied), str(token)):
                return await self._deny(send)
            return await self.app(scope, receive, send)
        # form-encoded: buffer the whole body, verify _csrf, then replay it
        msgs, body, more = [], b"", True
        while more:
            m = await receive(); msgs.append(m)
            body += m.get("body", b""); more = m.get("more_body", False)
        try:
            supplied = (parse_qs(body.decode("utf-8")).get("_csrf", [""]) or [""])[0]
        except Exception:
            supplied = ""
        if not token or not _secrets.compare_digest(str(supplied), str(token)):
            return await self._deny(send)
        i = 0

        async def replay():
            nonlocal i
            if i < len(msgs):
                i += 1
                return msgs[i - 1]
            return {"type": "http.request", "body": b"", "more_body": False}

        return await self.app(scope, replay, send)

    async def _deny(self, send):
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"CSRF validation failed"}'})


app.add_middleware(_CSRFMiddleware)


def brand() -> str:
    return config.load().get("app_name") or "HR Assistant"


def current_user(request: Request):
    """Signed-in end user, with role override applied and revocation honoured."""
    s = _sess(request)
    if not s or not s.get("identity"):
        return None
    ident = s["identity"]
    email = ident.get("email", "")
    if email and users_store.is_revoked(email):
        s.pop("identity", None)
        return None
    role = (users_store.get_override(email) if email else "") or ident.get("role", "employee")
    u = dict(ident); u["role"] = role
    return u


def is_admin(request: Request) -> bool:
    s = _sess(request)
    return bool(s and s.get("admin"))


def _bind_identity(u: dict | None):
    """Set the per-request identity + Oracle auth so tools run as this user."""
    idmod.set_current(u or {})
    idmod.set_current_auth((u or {}).get("_auth") or {})


def _flash(request):
    return request.query_params.get("msg", ""), request.query_params.get("err", "")


# ================================================================ END-USER ====
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if request.query_params.get("guest"):
        if (connectors.primary() or {}).get("type") == "oracle_hcm":   # fail closed on live data
            return HTMLResponse(ui.login_page(brand(),
                                error="Guest access is disabled for live Oracle HCM — please sign in."))
        u = {"display_name": "Guest", "role": "employee", "person_id": None,
             "person_number": None, "email": "", "resolved": False, "_auth": {}}
        resp = RedirectResponse("/", status_code=303)
        sid = _new_session(resp); SESSIONS[sid]["identity"] = u
        return resp
    per_user = config.as_bool(config.load().get("per_user_auth", "false")) and \
        bool((connectors.primary() or {}).get("type") == "oracle_hcm")
    return HTMLResponse(ui.login_page(brand(), per_user=per_user, error=request.query_params.get("err", "")))


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, identifier: str = Form(""), password: str = Form("")):
    ip = _client_ip(request)
    if _auth_locked(ip):
        return HTMLResponse(ui.login_page(brand(), error="Too many attempts — try again in a few minutes."),
                            status_code=429)
    if len(identifier) > 320 or len(password) > 256:
        return HTMLResponse(ui.login_page(brand(), error="Invalid input."), status_code=400)
    ident = None
    try:
        ident = await idmod.whoami(identifier, password=(password or None))
    except Exception:
        ident = None
    if not ident:
        _auth_fail(ip)
        return HTMLResponse(ui.login_page(brand(),
                            per_user=bool(password) or config.as_bool(config.load().get("per_user_auth", "false")),
                            error=f"Couldn't find “{identifier}”. Check the value or continue as guest."))
    resp = RedirectResponse("/", status_code=303)
    _rotate(request, resp, identity=ident)   # fresh session id on login (anti-fixation)
    try:
        users_store.upsert(ident.get("email") or identifier, ident.get("display_name") or identifier,
                           ident.get("person_number"), ident.get("role", "employee"))
    except Exception:
        pass
    return resp


@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get(COOKIE)
    if sid:
        SESSIONS.pop(sid, None)                # destroy the whole server-side session
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


def _page(request, active, title, body):
    u = current_user(request)
    return HTMLResponse(ui.doc(brand(), title, ui.enduser_nav(u["role"], active), body, user=u,
                               flash=request.query_params.get("msg", ""), csrf=_session_csrf(request)))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    role = u["role"]
    quick = [{"title": "My Profile", "desc": "Your job, department, manager and contact details.", "href": "/me"},
             {"title": "Ask the Assistant", "desc": "Chat with the AI over Oracle HCM.", "href": "/chat"},
             {"title": "Company Directory", "desc": "Find colleagues across the org.", "href": "/directory"}]
    if role_rules.can(role, "my_team"):
        quick.insert(1, {"title": "My Team", "desc": "Your direct reports and their details.", "href": "/team"})
    if role_rules.can(role, "reports_analytics"):
        quick.append({"title": "Reports", "desc": "Headcount and span-of-control analytics.", "href": "/reports"})
    banner = ""
    if role == "hr_admin":
        banner = '<div class="banner">🔓 You have <b>HR-wide access</b>. Oracle HCM governs exactly which records you can see.</div>'
    elif role == "manager":
        banner = f'<div class="banner">👥 Manager view — you can see your team ({u.get("report_count",0)} direct).</div>'
    body = (banner + f'<h1>Good day, {ui.esc(u["display_name"])} 👋</h1>'
            f'<p class="sub">You are signed in as <b>{ui.ROLE_LABEL.get(role, role)}</b>. '
            "Here's what you can do:</p>" + ui.cards(quick)
            + '<div class="panel">Ask me anything — e.g. '
              '<a href="/chat">“Who is my manager?”</a> · '
              '<a href="/chat">“Who is on my team?”</a> · '
              '<a href="/directory">search the directory</a>.</div>')
    return _page(request, "/", "Home", body)


CHAT_JS = """
<script>
const log=document.getElementById('log'), inp=document.getElementById('inp'), btn=document.getElementById('send');
const CSRF=(document.getElementById('csrf')||{}).value||'';
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function add(cls,html,meta){const d=document.createElement('div');d.className='msg '+cls;
 d.innerHTML='<div class="bub">'+html+'</div>'+(meta?'<div class="meta">'+esc(meta)+'</div>':'');log.appendChild(d);log.scrollTop=log.scrollHeight;}
async function ask(){const q=inp.value.trim();if(!q)return;add('u',esc(q));inp.value='';btn.disabled=true;
 add('a','<i>thinking…</i>');const ph=log.lastChild;
 try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},body:JSON.stringify({message:q})});
 const j=await r.json();ph.remove();
 const meta=(j.tools&&j.tools.length)?('🔧 '+j.tools.join(', ')+' · '+(j.source||'')):'';
 add('a',esc(j.answer||j.error||'(no answer)').replace(/\\n/g,'<br>'),meta);}
 catch(e){ph.remove();add('a',esc('⚠️ '+e));}finally{btn.disabled=false;inp.focus();}}
inp.addEventListener('keydown',e=>{if(e.key==='Enter')ask();});
</script>"""


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    body = (f'<h1>Assistant</h1><p class="sub">Mode: {ui.esc(active_mode())}. '
            "Tools and data are scoped to your role by the backend; Oracle enforces access.</p>"
            f'<input type="hidden" id="csrf" value="{ui.esc(_session_csrf(request))}">'
            '<div class="chatlog" id="log"></div>'
            '<div class="inbar"><input id="inp" placeholder="Ask about a person, your team, a department…" autofocus>'
            '<button class="btn" id="send" onclick="ask()">Send</button></div>' + CHAT_JS)
    return _page(request, "/chat", "Assistant", body)


@app.get("/me", response_class=HTMLResponse)
async def me_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    _bind_identity(u)
    if not (u.get("person_id") or u.get("person_number")):
        return _page(request, "/me", "My Profile",
                     '<h1>My Profile</h1><div class="panel muted">No linked Oracle record (guest session).</div>')
    w = await sources.get_worker(u.get("person_id") or u.get("person_number"))
    body = "<h1>My Profile</h1>" + _worker_card(w, u["role"], is_self=True)
    return _page(request, "/me", "My Profile", body)


def _worker_card(w: dict, role: str, is_self=False) -> str:
    if not w or w.get("error"):
        return '<div class="panel muted">Record not available.</div>'
    fields = [("Name", w.get("DisplayName")), ("Job title", w.get("JobTitle")),
              ("Department", w.get("DepartmentName")), ("Location", w.get("LocationName")),
              ("Manager", w.get("ManagerName")), ("Work email", w.get("WorkEmail")),
              ("Employee #", w.get("PersonNumber")), ("Status", w.get("AssignmentStatus"))]
    # field policy (light UX filter; Oracle is the real gate)
    sens = {"WorkPhoneNumber": "home_phone"}
    if role_rules.visible_field(role, "home_phone", is_self) and w.get("WorkPhoneNumber"):
        fields.insert(6, ("Phone", w.get("WorkPhoneNumber")))
    pairs = [(k, ui.esc(v) if v else '<span class="muted">—</span>') for k, v in fields]
    return ui.kv(pairs)


@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    if not role_rules.can(u["role"], "my_team"):
        return _page(request, "/team", "My Team",
                     '<h1>My Team</h1><div class="banner">This view is for managers. '
                     'You don\'t have direct reports on record.</div>')
    _bind_identity(u)
    reps = await sources.get_direct_reports(u.get("person_id") or u.get("person_number"))
    rows = [[ui.esc(r.get("DisplayName")), ui.esc(r.get("JobTitle") or "—"),
             ui.esc(r.get("DepartmentName") or "—"),
             f'<a href="/directory?q={quote(str(r.get("DisplayName") or ""))}">view</a>'] for r in (reps or [])]
    body = (f'<h1>My Team</h1><p class="sub">{len(reps or [])} direct report(s) for '
            f'{ui.esc(u["display_name"])}.</p>' + ui.table(["Name", "Title", "Department", ""], rows))
    return _page(request, "/team", "My Team", body)


@app.get("/directory", response_class=HTMLResponse)
async def directory_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    _bind_identity(u)
    q = request.query_params.get("q", "").strip()
    rows = []
    if q:
        res = await sources.search(q)
        for r in (res or []):
            rows.append([ui.esc(r.get("DisplayName")), ui.esc(r.get("JobTitle") or "—"),
                         ui.esc(r.get("DepartmentName") or "—"), ui.esc(r.get("WorkEmail") or "—")])
    scope = "full records (HR access)" if u["role"] == "hr_admin" else "public directory fields"
    body = (f'<h1>Directory</h1><p class="sub">Showing {ui.esc(scope)}. Oracle returns only what you may see.</p>'
            f'<form method="get" class="row" style="margin-bottom:14px">'
            f'<input name="q" value="{ui.esc(q)}" placeholder="Search name, email, department, title…" style="flex:3">'
            f'<button class="btn">Search</button></form>')
    if q:
        body += ui.table(["Name", "Title", "Department", "Email"], rows)
    else:
        body += '<div class="panel muted">Type a name or department above to search.</div>'
    return _page(request, "/directory", "Directory", body)


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    if not role_rules.can(u["role"], "reports_analytics"):
        return _page(request, "/reports", "Reports",
                     '<h1>Reports</h1><div class="banner">Reports are available to HR Admins.</div>')
    _bind_identity(u)
    ov = await sources.overview()
    body = f'<h1>Reports</h1><p class="sub">Total workers visible: <b>{ov.get("total",0)}</b>.</p>'
    if ov.get("note"):
        body += f'<div class="banner">{ui.esc(ov["note"])}</div>'
    if ov.get("by_department"):
        rows = [[ui.esc(d["department"]), str(d["count"])] for d in ov["by_department"]]
        body += "<h2>Headcount by department</h2>" + ui.table(["Department", "Headcount"], rows)
    if ov.get("spans"):
        rows = [[ui.esc(s["manager"]), ui.esc(s["title"]), str(s["reports"])] for s in ov["spans"]]
        body += "<h2>Span of control</h2>" + ui.table(["Manager", "Title", "Direct reports"], rows)
    return _page(request, "/reports", "Reports", body)


@app.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    if not role_rules.can(u["role"], "approvals"):
        return _page(request, "/approvals", "Approvals",
                     '<h1>Approvals</h1><div class="banner">Approvals are for managers and HR.</div>')
    body = ('<h1>Approvals</h1><div class="panel muted">No pending approvals. '
            "Leave and data-change approvals arrive with the ServiceNow connector (see Admin → Data Sources).</div>")
    return _page(request, "/approvals", "Approvals", body)


@app.get("/policies", response_class=HTMLResponse)
async def policies_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    body = ('<h1>Policies & Help</h1>'
            '<div class="panel">Policy Q&A is powered by a <b>Knowledge Base connector</b> '
            '(planned — configure under Admin → Data Sources). Meanwhile, ask the '
            '<a href="/chat">Assistant</a> about people and teams.</div>')
    return _page(request, "/policies", "Policies", body)


# ------------------------------------------------------------ END-USER APIs ---
@app.get("/api/session")
async def api_session(request: Request):
    u = current_user(request)
    if not u:
        return JSONResponse({"authenticated": False})
    return JSONResponse({"authenticated": True, "mode": active_mode(),
                         "identity": {k: u.get(k) for k in
                                      ("display_name", "role", "person_id", "person_number", "email", "report_count")}})


def _tools_used(messages):
    names = []
    for m in messages:
        for p in getattr(m, "parts", []):
            if p.__class__.__name__ == "ToolCallPart" and p.tool_name not in names:
                names.append(p.tool_name)
    return names


@app.post("/api/chat")
async def api_chat(request: Request):
    u = current_user(request)
    if not u:
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    data = await request.json()
    msg = (data.get("message") or "").strip()
    if not msg:
        return JSONResponse({"error": "Empty message."}, status_code=400)
    if len(msg) > MAX_MSG_LEN:
        return JSONResponse({"error": "Message too long."}, status_code=413)
    _bind_identity(u)
    s = _sess(request); hist = (s.get("history", []) if s else [])[-MAX_HISTORY:]
    try:
        agent = build_agent(identity=u)
        async with agent:
            result = await agent.run(msg, message_history=hist)
        if s is not None:
            s["history"] = result.all_messages()[-MAX_HISTORY:]
        return JSONResponse({"answer": result.output, "tools": _tools_used(result.all_messages()),
                             "source": (connectors.primary() or {}).get("name", "")})
    except Exception as e:
        ref = _secrets.token_hex(4)
        print(f"[chat-error {ref}] {type(e).__name__}: {e}")   # detail server-side only
        return JSONResponse({"error": f"Something went wrong (ref {ref})."}, status_code=500)


@app.get("/api/me")
async def api_me(request: Request):
    u = current_user(request)
    if not u:
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    _bind_identity(u)
    pid = u.get("person_id") or u.get("person_number")
    return JSONResponse(await sources.get_worker(pid) if pid else {"error": "no_record"})


@app.get("/api/team")
async def api_team(request: Request):
    u = current_user(request)
    if not u:
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    if not role_rules.can(u["role"], "my_team"):
        return JSONResponse({"reports": [], "note": "Managers only."})
    _bind_identity(u)
    reps = await sources.get_direct_reports(u.get("person_id") or u.get("person_number"))
    return JSONResponse({"reports": reps or []})


@app.get("/api/directory")
async def api_directory(request: Request):
    u = current_user(request)
    if not u:
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    _bind_identity(u)
    return JSONResponse({"results": await sources.search(request.query_params.get("q", "").strip())})


@app.get("/api/reports")
async def api_reports(request: Request):
    u = current_user(request)
    if not u or not role_rules.can(u["role"], "reports_analytics"):
        return JSONResponse({"error": "HR only."}, status_code=403)
    _bind_identity(u)
    return JSONResponse(await sources.overview())


# =================================================================== ADMIN ====
def _admin_guard(request: Request):
    return None if is_admin(request) else RedirectResponse("/admin/login", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_get(request: Request):
    return HTMLResponse(ui.login_page(brand(), admin=True, error=request.query_params.get("err", "")))


@app.post("/admin/login")
async def admin_login_post(request: Request, identifier: str = Form(""), password: str = Form("")):
    cfg = config.load()
    ip = _client_ip(request)
    allow = {x.strip().lower() for x in (cfg.get("platform_admin_emails", "") or "")
             .replace(";", ",").split(",") if x.strip()}
    email = (identifier or "").strip().lower()
    pwhash = cfg.get("admin_password_hash", "")

    def _deny(m):
        _auth_fail(ip)
        log.warning("admin login denied ip=%s email=%s (%s)", ip, email, m)
        return HTMLResponse(ui.login_page(brand(), admin=True, error=m), status_code=403)

    if _auth_locked(ip):
        return HTMLResponse(ui.login_page(brand(), admin=True, error="Too many attempts — try again shortly."),
                            status_code=429)
    if pwhash:                                   # a password is configured → it must match
        if not password or not _secrets.compare_digest(_pwhash(password, cfg.get("admin_salt", "")), pwhash):
            return _deny("Incorrect admin password.")
    elif not _client_is_local(request):          # no password set → local machine only
        return _deny("Admin sign-in must be from this machine, or set an admin password in Settings first.")
    if allow and email not in allow:
        return _deny("Not on the Platform Admin allow-list.")

    resp = RedirectResponse("/admin", status_code=303)
    ident = (_sess(request) or {}).get("identity")   # keep any end-user identity while elevating
    _rotate(request, resp, admin=True, admin_email=(email or "admin"), identity=ident)
    audit_store.log(email or "admin", "platform_admin", "admin_login", "console", ip, "ok", 0)
    log.info("admin login ok ip=%s email=%s", ip, email)
    return resp


@app.get("/admin/logout")
async def admin_logout(request: Request):
    s = _sess(request)
    if s:
        s.pop("admin", None); s.pop("admin_email", None)
    return RedirectResponse("/admin/login", status_code=303)


def _admin_page(request, active, title, body):
    s = _sess(request) or {}
    admin_user = {"display_name": s.get("admin_email", "admin"), "role": "platform_admin"}
    msg, err = _flash(request)
    return HTMLResponse(ui.doc(brand(), title, ui.admin_nav(active), body, user=admin_user, admin=True,
                               flash=msg or err, flash_err=bool(err), csrf=_session_csrf(request)))


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    cons = connectors.masked_list()
    st = audit_store.stats(); cs = cache.stats()
    tools = registry.list_all(); enabled = sum(1 for t in tools if t["enabled"])
    src_html = " ".join(
        (ui.badge(c["name"] + (" ★" if c["is_primary"] else ""), "ok") if c["enabled"]
         else ui.badge(c["name"] + " (off)", "off")) for c in cons)
    cards = [{"title": "Requests (24h)", "desc": str(st["today"])},
             {"title": "Errors (all)", "desc": str(st["errors"])},
             {"title": "Avg latency", "desc": f'{st["avg_latency_ms"]} ms'},
             {"title": "Cache hit-rate", "desc": f'{int(cs["hit_rate"]*100)}%  ({cs["entries"]} entries)'},
             {"title": "Tools enabled", "desc": f'{enabled}/{len(tools)}'},
             {"title": "AI / Data", "desc": active_mode()}]
    err_rows = [[e["when"], ui.esc(e["tool"]), ui.esc(e["result"]), ui.esc(e["source"])]
                for e in st["recent_errors"]] or []
    body = (f'<h1>Dashboard</h1><p class="sub">Platform health at a glance.</p>'
            f'<div class="panel"><b>Data sources:</b> {src_html or "none"}</div>'
            + ui.cards(cards)
            + "<h2>Recent errors</h2>" + ui.table(["When", "Tool", "Result", "Source"], err_rows))
    return _admin_page(request, "/admin", "Dashboard", body)


@app.get("/admin/sources", response_class=HTMLResponse)
async def admin_sources(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    rows = []
    for c in connectors.masked_list():
        status = ui.badge("enabled", "ok") if c["enabled"] else ui.badge("disabled", "off")
        prim = ui.badge("primary", "role") if c["is_primary"] else ""
        acts = (f'<form method="post" action="/admin/sources/{c["id"]}/test" style="display:inline">'
                f'<button class="btn sm grey">Test</button></form> '
                f'<a class="btn sm ghost" href="/admin/sources/{c["id"]}">Edit</a> ')
        if not c["is_primary"]:
            acts += (f'<form method="post" action="/admin/sources/{c["id"]}/primary" style="display:inline">'
                     f'<button class="btn sm">Make primary</button></form> '
                     f'<form method="post" action="/admin/sources/{c["id"]}/delete" style="display:inline">'
                     f'<button class="btn sm danger">Delete</button></form>')
        rows.append([ui.esc(c["name"]), ui.esc(c["type"]), f"{status} {prim}", acts])
    addlinks = " ".join(f'<a class="btn sm ghost" href="/admin/sources/new?type={t["type"]}">+ {ui.esc(t["label"])}</a>'
                        for t in conn_types.list_types())
    body = (f'<h1>Data Sources</h1><p class="sub">Connectors that feed the assistant. The <b>primary</b> '
            "drives chat; others can contribute namespaced tools.</p>"
            + ui.table(["Name", "Type", "Status", "Actions"], rows)
            + f'<div class="panel"><b>Add a source:</b><br><br>{addlinks}</div>')
    return _admin_page(request, "/admin/sources", "Data Sources", body)


def _source_form(c, typ):
    meta = conn_types.get_type(typ) or {"fields": [], "label": typ}
    fields_html = f'<input type="hidden" name="type" value="{ui.esc(typ)}">'
    if c:
        fields_html += f'<input type="hidden" name="id" value="{ui.esc(c["id"])}">'
    fields_html += f'<label>Display name</label><input name="name" value="{ui.esc(c["name"] if c else meta["label"])}">'
    cfg = (c or {}).get("config", {})
    for f in meta["fields"]:
        val = cfg.get(f["key"], "")
        if f["type"] == "select":
            opts = "".join(f'<option {"selected" if val==o else ""}>{ui.esc(o)}</option>' for o in f["options"])
            fields_html += f'<label>{ui.esc(f["label"])}</label><select name="cfg_{f["key"]}">{opts}</select>'
        else:
            typ_attr = "password" if f["type"] == "password" else "text"
            ph = f.get("placeholder", "")
            shown = "" if f["type"] == "password" else ui.esc(val)
            fields_html += (f'<label>{ui.esc(f["label"])}</label>'
                            f'<input name="cfg_{f["key"]}" type="{typ_attr}" value="{shown}" placeholder="{ui.esc(ph)}">')
    en = "checked" if (not c or c.get("enabled")) else ""
    pr = "checked" if (c and c.get("is_primary")) else ""
    fields_html += (f'<label><input type="checkbox" name="enabled" {en} style="width:auto"> Enabled</label>'
                    f'<label><input type="checkbox" name="is_primary" {pr} style="width:auto"> Primary</label>')
    return (f'<div class="panel"><form method="post" action="/admin/sources/save">{fields_html}'
            f'<br><button class="btn">Save source</button> '
            f'<a class="btn grey" href="/admin/sources">Cancel</a></form></div>')


@app.get("/admin/sources/new", response_class=HTMLResponse)
async def admin_source_new(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    typ = request.query_params.get("type", "oracle_hcm")
    return _admin_page(request, "/admin/sources", "New Data Source",
                       f'<h1>New {ui.esc((conn_types.get_type(typ) or {}).get("label", typ))}</h1>'
                       + _source_form(None, typ))


@app.get("/admin/sources/{cid}", response_class=HTMLResponse)
async def admin_source_edit(request: Request, cid: str):
    g = _admin_guard(request)
    if g:
        return g
    c = next((x for x in connectors.masked_list() if x["id"] == cid), None)
    if not c:
        return RedirectResponse("/admin/sources?err=Source+not+found", status_code=303)
    return _admin_page(request, "/admin/sources", "Edit Data Source",
                       f'<h1>Edit {ui.esc(c["name"])}</h1>' + _source_form(c, c["type"]))


@app.post("/admin/sources/save")
async def admin_source_save(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    cfg = {k[4:]: v for k, v in form.items() if k.startswith("cfg_")}
    d = {"name": form.get("name", "Source"), "type": form.get("type", "oracle_hcm"),
         "enabled": 1 if form.get("enabled") else 0, "is_primary": 1 if form.get("is_primary") else 0,
         "config": cfg}
    if form.get("id"):
        d["id"] = form.get("id")
    connectors.upsert(d)
    return RedirectResponse("/admin/sources?msg=Saved", status_code=303)


@app.post("/admin/sources/{cid}/test")
async def admin_source_test(request: Request, cid: str):
    g = _admin_guard(request)
    if g:
        return g
    c = connectors.get(cid)
    if not c:
        return RedirectResponse("/admin/sources?err=Not+found", status_code=303)
    r = await sources.test(c)
    if r.get("ok"):
        return RedirectResponse(f'/admin/sources?msg=OK+(status+{r.get("status","")},+count+{r.get("count","?")})', 303)
    return RedirectResponse(f'/admin/sources?err={quote(str(r.get("error","failed")))}', status_code=303)


@app.post("/admin/sources/{cid}/primary")
async def admin_source_primary(request: Request, cid: str):
    g = _admin_guard(request)
    if g:
        return g
    connectors.set_primary(cid)
    return RedirectResponse("/admin/sources?msg=Primary+updated", status_code=303)


@app.post("/admin/sources/{cid}/delete")
async def admin_source_delete(request: Request, cid: str):
    g = _admin_guard(request)
    if g:
        return g
    connectors.delete(cid)
    return RedirectResponse("/admin/sources?msg=Deleted", status_code=303)


def _tool_form(t):
    """Render the tool create/edit form. t=None → new tool."""
    is_new = t is None
    builtin = bool(t and t["builtin"])
    roles = t["allowed_roles"] if t else ["employee", "manager", "hr_admin"]
    roles_boxes = "".join(
        f'<label style="font-weight:400;display:inline-block;margin-right:14px">'
        f'<input type="checkbox" name="role_{r}" {"checked" if r in roles else ""} style="width:auto"> {r}</label>'
        for r in ("employee", "manager", "hr_admin"))

    def sel(nm, opts, cur):
        o = "".join(f'<option {"selected" if cur == x else ""}>{x}</option>' for x in opts)
        return f'<select name="{nm}">{o}</select>'

    name_field = (f'<input name="name" value="{ui.esc(t["name"]) if t else ""}" '
                  f'{"readonly" if not is_new else ""} placeholder="e.g. get_leave_balance">')
    params = json.dumps(t["params"]) if t else '{"limit": 50}'
    rmap = json.dumps(t["result_map"]) if t else '{}'
    note = ('<div class="banner">Built-in tool — its implementation is fixed. You can change roles, '
            'cache, PII and enable/disable.</div>' if builtin else
            '<div class="banner">Config tool — define it entirely here; it becomes callable by the agent '
            'immediately, no code or restart.</div>')
    adv = "" if builtin else (
        f'<label>Kind</label>{sel("kind", ["oracle_child", "oracle_search", "oracle_detail"], t["kind"] if t else "oracle_child")}'
        f'<label>Argument the agent supplies</label>{sel("arg", ["person_id", "query"], t["arg"] if t else "person_id")}'
        f'<label>Endpoint — child resource for oracle_child (e.g. <span class="mono">absences</span>, '
        f'<span class="mono">salaries</span>, <span class="mono">phones</span>)</label>'
        f'<input name="endpoint" value="{ui.esc(t["endpoint"]) if t else ""}">'
        f'<label>Params (JSON)</label><textarea name="params" rows="2" class="mono">{ui.esc(params)}</textarea>'
        f'<label>Result map (JSON <span class="mono">{{"OutLabel":"OracleField"}}</span>, empty = raw)</label>'
        f'<textarea name="result_map" rows="2" class="mono">{ui.esc(rmap)}</textarea>')
    return (note + '<div class="panel"><form method="post" action="/admin/tools/save">'
            f'<label>Tool name</label>{name_field}'
            f'<label>Description (the agent uses this to decide when to call it)</label>'
            f'<input name="description" value="{ui.esc(t["description"]) if t else ""}">'
            f'<label>Source type</label><input name="source_type" value="{ui.esc(t["source_type"]) if t else "oracle_hcm"}">'
            f'<b>Allowed roles</b><br>{roles_boxes}'
            f'<label>Cache TTL (seconds)</label><input name="cache_ttl" value="{t["cache_ttl"] if t else 300}" style="max-width:160px">'
            f'<label>PII level</label>{sel("pii_level", ["low", "medium", "high"], t["pii_level"] if t else "low")}'
            f'<label style="display:inline-block"><input type="checkbox" name="enabled" '
            f'{"checked" if (is_new or t["enabled"]) else ""} style="width:auto"> Enabled</label>'
            + adv +
            '<br><br><button class="btn">Save tool</button> '
            '<a class="btn grey" href="/admin/tools">Cancel</a></form></div>')


@app.get("/admin/tools", response_class=HTMLResponse)
async def admin_tools(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    rows = []
    for t in registry.list_all():
        en = (f'<form method="post" action="/admin/tools/{t["name"]}/toggle" style="display:inline">'
              f'<button class="btn sm {"grey" if t["enabled"] else ""}">'
              f'{"Disable" if t["enabled"] else "Enable"}</button></form>')
        dele = ("" if t["builtin"] else
                f' <form method="post" action="/admin/tools/{t["name"]}/delete" style="display:inline">'
                f'<button class="btn sm danger">Delete</button></form>')
        name = ui.esc(t["name"]) + (" " + ui.badge("ns", "role") if t["namespaced"] else "")
        kind = "built-in" if t["builtin"] else ui.esc(t["kind"])
        rows.append([name, ui.esc(t["source_type"]), kind,
                     ui.badge("on", "ok") if t["enabled"] else ui.badge("off", "off"),
                     ", ".join(t["allowed_roles"]),
                     ui.badge(t["pii_level"], "pii") if t["pii_level"] == "high" else t["pii_level"],
                     f'{t["cache_ttl"]}s',
                     f'<a class="btn sm ghost" href="/admin/tools/{t["name"]}">Edit</a> {en}{dele}'])
    body = ('<h1>MCP Tools</h1>'
            '<div style="float:right;margin-top:-38px"><a class="btn" href="/admin/tools/new">＋ New tool</a></div>'
            '<p class="sub">The governed tool registry. The agent is handed only tools that are '
            "<b>enabled</b> and <b>allowed for the user's role</b>. Add a tool below — no code, no restart. "
            "Oracle still enforces the data.</p>"
            + ui.table(["Tool", "Source", "Kind", "Enabled", "Allowed roles", "PII", "Cache", "Actions"], rows))
    return _admin_page(request, "/admin/tools", "MCP Tools", body)


@app.get("/admin/tools/new", response_class=HTMLResponse)
async def admin_tool_new(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    return _admin_page(request, "/admin/tools", "New MCP Tool", "<h1>New MCP Tool</h1>" + _tool_form(None))


@app.post("/admin/tools/save")
async def admin_tool_save(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/admin/tools?err=Tool+name+required", status_code=303)
    d = {"name": name, "description": form.get("description", ""),
         "source_type": form.get("source_type") or "oracle_hcm",
         "allowed_roles": [r for r in ("employee", "manager", "hr_admin") if form.get(f"role_{r}")],
         "cache_ttl": form.get("cache_ttl") or 300, "pii_level": form.get("pii_level") or "low",
         "enabled": 1 if form.get("enabled") else 0}
    for k in ("kind", "arg", "endpoint"):
        if form.get(k) is not None:
            d[k] = form.get(k)
    for k in ("params", "result_map"):
        v = form.get(k)
        if v is not None:
            try:
                json.loads(v or "{}")
            except Exception:
                return RedirectResponse(f"/admin/tools?err=Invalid+JSON+in+{k}", status_code=303)
            d[k] = v or "{}"
    try:
        registry.upsert(d)
        sync_custom_tools()
    except Exception as e:
        return RedirectResponse(f"/admin/tools?err={quote(str(e))}", status_code=303)
    return RedirectResponse("/admin/tools?msg=Tool+saved", status_code=303)


@app.post("/admin/tools/{name}/toggle")
async def admin_tool_toggle(request: Request, name: str):
    g = _admin_guard(request)
    if g:
        return g
    t = registry.get(name)
    if t:
        registry.set_enabled(name, not t["enabled"])
        sync_custom_tools()
    return RedirectResponse("/admin/tools?msg=Updated", status_code=303)


@app.post("/admin/tools/{name}/delete")
async def admin_tool_delete(request: Request, name: str):
    g = _admin_guard(request)
    if g:
        return g
    if registry.delete(name):
        sync_custom_tools()
        return RedirectResponse("/admin/tools?msg=Tool+deleted", status_code=303)
    return RedirectResponse("/admin/tools?err=Cannot+delete+a+built-in+tool", status_code=303)


@app.get("/admin/tools/{name}", response_class=HTMLResponse)
async def admin_tool_detail(request: Request, name: str):
    g = _admin_guard(request)
    if g:
        return g
    t = registry.get(name)
    if not t:
        return RedirectResponse("/admin/tools?err=Unknown+tool", status_code=303)
    return _admin_page(request, "/admin/tools", "Edit MCP Tool",
                       f'<h1>Tool · {ui.esc(t["name"])}</h1>'
                       f'<p class="sub mono">{ui.esc(t["rest_mapping"])}</p>' + _tool_form(t))


@app.get("/admin/models", response_class=HTMLResponse)
async def admin_models(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    cfg = config.masked()
    prov = cfg.get("ai_provider", "demo")
    provsel = "".join(f'<option {"selected" if prov==p else ""}>{p}</option>'
                      for p in ("demo", "google", "openai", "anthropic"))
    loaded = ""
    if request.query_params.get("load"):
        r = await models_mod.list_models(config.load().get("ai_provider"), config.load().get("ai_api_key"))
        if r.get("ok") and r["models"]:
            loaded = ('<label>Available models (from provider)</label><select name="ai_model">'
                      + "".join(f'<option {"selected" if config.load().get("ai_model")==m else ""}>{ui.esc(m)}</option>'
                                for m in r["models"]) + "</select>")
        else:
            loaded = f'<div class="flash err">{ui.esc(r.get("error","No models"))}</div>'
    body = (f'<h1>AI Models</h1><p class="sub">Model-agnostic router. Active: {ui.esc(active_mode())}.</p>'
            '<div class="panel"><form method="post" action="/admin/models">'
            f'<label>Provider</label><select name="ai_provider">{provsel}</select>'
            f'<label>API key</label><input name="ai_api_key" type="password" '
            f'placeholder="{"•••• stored" if cfg.get("ai_api_key") else "paste key"}">'
            + (loaded or f'<label>Model</label><input name="ai_model" value="{ui.esc(config.load().get("ai_model",""))}" '
               'placeholder="leave blank for provider default">')
            + '<br><br><button class="btn">Save</button> '
            '<a class="btn grey" href="/admin/models?load=1">Load models from provider</a>'
            '</form></div>'
            '<div class="panel muted">Demo mode needs no key — it runs an offline scripted brain so the '
            "whole pipeline works without a provider.</div>")
    return _admin_page(request, "/admin/models", "AI Models", body)


@app.post("/admin/models")
async def admin_models_save(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    config.save({k: form.get(k, "") for k in ("ai_provider", "ai_api_key", "ai_model")})
    return RedirectResponse("/admin/models?msg=Saved", status_code=303)


@app.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    cfg = config.load()
    caps = role_rules.capabilities()
    fp = role_rules.field_policy()

    def opt(cur, o):
        return f'<option value="{o}" {"selected" if cur == o else ""}>{o}</option>'

    cap_form = '<form method="post" action="/admin/roles/caps"><div class="panel">'
    for cap, req in caps.items():
        cap_form += (f'<label>{ui.esc(cap)}</label><select name="cap_{ui.esc(cap)}" style="max-width:220px">'
                     + "".join(opt(req, o) for o in ("all", "manager", "hr_admin")) + "</select>")
    cap_form += '<br><br><button class="btn">Save capabilities</button></div></form>'

    fp_form = '<form method="post" action="/admin/roles/fields"><div class="panel">'
    for field, allowed in fp.items():
        boxes = "".join(
            f'<label style="font-weight:400;display:inline-block;margin-right:12px">'
            f'<input type="checkbox" name="fp_{ui.esc(field)}_{r}" {"checked" if r in allowed else ""} '
            f'style="width:auto"> {r}</label>' for r in ("employee", "manager", "hr_admin"))
        fp_form += f'<label>{ui.esc(field)}</label>{boxes}'
    fp_form += '<br><br><button class="btn">Save field policy</button></div></form>'

    body = (f'<h1>Roles & Access</h1><p class="sub">Role precedence: HR Admin &gt; Manager &gt; Employee. '
            "Roles are inferred from Oracle; these settings tailor the experience — everything here is editable.</p>"
            '<div class="panel"><form method="post" action="/admin/roles">'
            f'<label>HR Admin override list (emails / employee #, comma-separated)</label>'
            f'<input name="hr_admin_emails" value="{ui.esc(cfg.get("hr_admin_emails",""))}">'
            f'<label>Per-user Oracle sign-in (each user authenticates as themselves)</label>'
            f'<select name="per_user_auth"><option value="false" {"selected" if not config.as_bool(cfg.get("per_user_auth")) else ""}>off</option>'
            f'<option value="true" {"selected" if config.as_bool(cfg.get("per_user_auth")) else ""}>on</option></select>'
            f'<label>HR visibility threshold (# non-report records that implies HR)</label>'
            f'<input name="hr_visibility_threshold" value="{ui.esc(cfg.get("hr_visibility_threshold","1"))}" style="max-width:160px">'
            f'<label>Require sign-in</label>'
            f'<select name="require_login"><option value="true" {"selected" if config.as_bool(cfg.get("require_login")) else ""}>yes</option>'
            f'<option value="false" {"selected" if not config.as_bool(cfg.get("require_login")) else ""}>no</option></select>'
            '<br><br><button class="btn">Save</button></form></div>'
            "<h2>Capability matrix (§6) — which role unlocks each capability</h2>" + cap_form
            + "<h2>Field-level policy (UX filter)</h2>"
            + '<div class="muted" style="margin-bottom:8px">Oracle enforces real access; this only tidies UI display.</div>'
            + fp_form)
    return _admin_page(request, "/admin/roles", "Roles & Access", body)


@app.post("/admin/roles")
async def admin_roles_save(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    config.save({k: form.get(k, "") for k in
                 ("hr_admin_emails", "per_user_auth", "hr_visibility_threshold", "require_login")})
    return RedirectResponse("/admin/roles?msg=Saved", status_code=303)


@app.post("/admin/roles/caps")
async def admin_roles_caps(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    caps = role_rules.capabilities()
    role_rules.save_capabilities({k: (form.get(f"cap_{k}") or v) for k, v in caps.items()})
    return RedirectResponse("/admin/roles?msg=Capabilities+saved", status_code=303)


@app.post("/admin/roles/fields")
async def admin_roles_fields(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    fp = role_rules.field_policy()
    new = {field: [r for r in ("employee", "manager", "hr_admin") if form.get(f"fp_{field}_{r}")]
           for field in fp}
    role_rules.save_field_policy(new)
    return RedirectResponse("/admin/roles?msg=Field+policy+saved", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    rows = []
    for us in users_store.list_all():
        sel = "".join(f'<option value="{r}" {"selected" if us["role_override"]==r else ""}>{r or "—"}</option>'
                      for r in ("", "employee", "manager", "hr_admin"))
        override = (f'<form method="post" action="/admin/users/override" style="display:inline">'
                    f'<input type="hidden" name="email" value="{ui.esc(us["email"])}">'
                    f'<select name="role" style="width:auto;display:inline">{sel}</select> '
                    f'<button class="btn sm">Set</button></form>')
        revoke = (f'<form method="post" action="/admin/users/revoke" style="display:inline">'
                  f'<input type="hidden" name="email" value="{ui.esc(us["email"])}">'
                  f'<button class="btn sm danger">Revoke</button></form>')
        rows.append([ui.esc(us["display_name"]), ui.esc(us["email"]),
                     ui.badge(us["effective_role"], "role"),
                     us["last_seen"] + (" " + ui.badge("revoked", "err") if us["revoked"] else ""),
                     override + " " + revoke])
    body = (f'<h1>Users & Sessions</h1><p class="sub">People who have signed in. Overriding a role changes '
            "the UX only — Oracle still governs the data.</p>"
            + ui.table(["Name", "Email", "Effective role", "Last seen", "Actions"], rows))
    return _admin_page(request, "/admin/users", "Users & Sessions", body)


@app.post("/admin/users/override")
async def admin_user_override(request: Request, email: str = Form(""), role: str = Form("")):
    g = _admin_guard(request)
    if g:
        return g
    users_store.set_override(email, role)
    return RedirectResponse("/admin/users?msg=Role+override+set", status_code=303)


@app.post("/admin/users/revoke")
async def admin_user_revoke(request: Request, email: str = Form("")):
    g = _admin_guard(request)
    if g:
        return g
    users_store.revoke(email)
    return RedirectResponse("/admin/users?msg=Session+revoked", status_code=303)


@app.get("/admin/cache", response_class=HTMLResponse)
async def admin_cache(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    cfg = config.load(); cs = cache.stats()
    persrc = ""
    for c in connectors.list_all():
        if c["type"] != "mock":
            persrc += (f'<form method="post" action="/admin/cache/clear" style="display:inline">'
                       f'<input type="hidden" name="prefix" value="hcm:{c["id"]}:">'
                       f'<button class="btn sm grey">Clear {ui.esc(c["name"])}</button></form> ')
    body = (f'<h1>Cache</h1><p class="sub">In-process TTL cache — speeds up repeated lookups.</p>'
            '<div class="panel"><form method="post" action="/admin/cache/save">'
            f'<label>Enabled</label><select name="cache_enabled">'
            f'<option value="true" {"selected" if config.as_bool(cfg.get("cache_enabled")) else ""}>yes</option>'
            f'<option value="false" {"selected" if not config.as_bool(cfg.get("cache_enabled")) else ""}>no</option></select>'
            f'<label>TTL (seconds)</label><input name="cache_ttl" value="{ui.esc(cfg.get("cache_ttl","300"))}" style="max-width:160px">'
            '<br><br><button class="btn">Save</button></form></div>'
            + ui.cards([{"title": "Entries", "desc": str(cs["entries"])},
                        {"title": "Hits", "desc": str(cs["hits"])},
                        {"title": "Misses", "desc": str(cs["misses"])},
                        {"title": "Hit-rate", "desc": f'{int(cs["hit_rate"]*100)}%'}])
            + '<div class="panel"><form method="post" action="/admin/cache/clear" style="display:inline">'
              '<button class="btn danger">Clear all</button></form> ' + persrc + "</div>")
    return _admin_page(request, "/admin/cache", "Cache", body)


@app.post("/admin/cache/save")
async def admin_cache_save(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    config.save({"cache_enabled": form.get("cache_enabled", "true"), "cache_ttl": form.get("cache_ttl", "300")})
    return RedirectResponse("/admin/cache?msg=Saved", status_code=303)


@app.post("/admin/cache/clear")
async def admin_cache_clear(request: Request, prefix: str = Form("")):
    g = _admin_guard(request)
    if g:
        return g
    n = cache.clear(prefix or None)
    return RedirectResponse(f"/admin/cache?msg=Cleared+{n}+entries", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    tool = request.query_params.get("tool", ""); role = request.query_params.get("role", "")
    result = request.query_params.get("result", "")
    entries = audit_store.list_recent(200, tool=tool, role=role, result=result)
    rows = [[e["when"], ui.esc(e["user"]), ui.badge(e["role"], "role"), ui.esc(e["tool"]),
             ui.esc(e["source"]), ui.esc(e["target_ref"]),
             (ui.badge(e["result"], "ok") if e["result"] == "ok" else ui.badge(e["result"], "err")),
             f'{e["latency_ms"]}ms'] for e in entries]
    body = (f'<h1>Audit Log</h1><p class="sub">Every tool access — user · role · tool · source · record · result. '
            '<a href="/admin/audit/export">Export CSV</a></p>'
            '<form method="get" class="row" style="margin-bottom:12px">'
            f'<input name="tool" value="{ui.esc(tool)}" placeholder="filter tool">'
            f'<input name="role" value="{ui.esc(role)}" placeholder="filter role">'
            f'<input name="result" value="{ui.esc(result)}" placeholder="ok / denied / error">'
            '<button class="btn">Filter</button></form>'
            + ui.table(["When", "User", "Role", "Tool", "Source", "Record", "Result", "Latency"], rows))
    return _admin_page(request, "/admin/audit", "Audit Log", body)


@app.get("/admin/audit/export")
async def admin_audit_export(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    return PlainTextResponse(audit_store.export_csv(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=audit.csv"})


@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    cfg = config.load()
    body = (f'<h1>Settings</h1><p class="sub">Branding, environment, admin gate and SSO (production).</p>'
            '<div class="panel"><form method="post" action="/admin/settings">'
            f'<label>App name (branding)</label><input name="app_name" value="{ui.esc(cfg.get("app_name","HR Assistant"))}">'
            f'<label>Environment</label><input name="environment" value="{ui.esc(cfg.get("environment","prototype"))}" style="max-width:220px">'
            f'<label>Platform Admin allow-list (emails, comma-separated — empty = open/bootstrap)</label>'
            f'<input name="platform_admin_emails" value="{ui.esc(cfg.get("platform_admin_emails",""))}">'
            f'<label>OIDC issuer (SSO, production)</label><input name="oidc_issuer" value="{ui.esc(cfg.get("oidc_issuer",""))}">'
            f'<label>OIDC client id</label><input name="oidc_client_id" value="{ui.esc(cfg.get("oidc_client_id",""))}">'
            f'<label>Admin password ({"set — blank keeps it" if cfg.get("admin_password_hash") else "not set — recommended"})</label>'
            f'<input name="admin_password" type="password" placeholder="leave blank to keep current">'
            f'<label>Bind host (127.0.0.1 = this machine only)</label>'
            f'<input name="bind_host" value="{ui.esc(cfg.get("bind_host","127.0.0.1"))}" style="max-width:220px">'
            f'<label>Secure cookie (enable only behind HTTPS)</label>'
            f'<select name="cookie_secure"><option value="false" {"selected" if not config.as_bool(cfg.get("cookie_secure")) else ""}>no</option>'
            f'<option value="true" {"selected" if config.as_bool(cfg.get("cookie_secure")) else ""}>yes</option></select>'
            '<br><br><button class="btn">Save</button></form></div>')
    return _admin_page(request, "/admin/settings", "Settings", body)


@app.post("/admin/settings")
async def admin_settings_save(request: Request):
    g = _admin_guard(request)
    if g:
        return g
    form = await request.form()
    config.save({k: form.get(k, "") for k in
                 ("app_name", "environment", "platform_admin_emails", "oidc_issuer", "oidc_client_id",
                  "bind_host", "cookie_secure")})
    pw = form.get("admin_password")
    if pw:                                     # set/replace admin password (blank keeps current)
        salt = config.load().get("admin_salt") or _secrets.token_hex(8)
        config.save({"admin_salt": salt, "admin_password_hash": _pwhash(pw, salt)})
    return RedirectResponse("/admin/settings?msg=Saved", status_code=303)


# ------------------------------------------------------------ admin JSON API --
@app.get("/api/admin/sources")
async def api_admin_sources(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return JSONResponse({"sources": connectors.masked_list(), "types": conn_types.list_types()})


@app.get("/api/admin/tools")
async def api_admin_tools(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return JSONResponse({"tools": registry.list_all()})


@app.get("/api/admin/models")
async def api_admin_models(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    c = config.masked()
    return JSONResponse({"provider": c.get("ai_provider"), "model": c.get("ai_model"), "mode": active_mode()})


@app.get("/api/admin/roles")
async def api_admin_roles(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return JSONResponse({"capabilities": role_rules.capabilities(), "field_policy": role_rules.field_policy()})


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return JSONResponse({"users": users_store.list_all()})


@app.get("/api/admin/cache")
async def api_admin_cache(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return JSONResponse(cache.stats())


@app.get("/api/admin/audit")
async def api_admin_audit(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return JSONResponse({"entries": audit_store.list_recent(200)})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mode": active_mode(), "primary": (connectors.primary() or {}).get("name")}


@app.get("/readyz")
async def readyz():
    detail = {}
    ready = True
    try:
        connectors.list_all(); detail["db"] = "ok"
    except Exception as e:
        ready = False; detail["db"] = f"{type(e).__name__}: {e}"
    detail["primary"] = (connectors.primary() or {}).get("name")
    return JSONResponse({"ready": ready, **detail}, status_code=200 if ready else 503)


def main():
    import uvicorn, os
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST") or config.load().get("bind_host", "127.0.0.1")
    print(f"HR Assistant  ->  http://localhost:{port}/")
    print(f"Admin Console ->  http://localhost:{port}/admin")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
