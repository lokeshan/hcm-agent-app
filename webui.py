"""Server-rendered UI for the two apps (§4/§5/§7).

webui builds HTML; server.py supplies the data and wires routes. Two visual
identities reinforce the separation: the End-User app (blue) and the Admin
Console (slate). Navigation is filtered by role.
"""
from __future__ import annotations
import html
import re
import role_rules

CSS = """
:root{--blue:#2554d6;--blue2:#1b3fa8;--slate:#1f2937;--slate2:#111827;--bg:#f5f7fb;
--card:#fff;--line:#e5e7eb;--muted:#6b7280;--ok:#16a34a;--warn:#b45309;--err:#dc2626;}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#111;background:var(--bg)}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.layout{display:flex;min-height:100vh}
.side{width:230px;flex:none;background:var(--slate);color:#cbd5e1;padding:0 0 20px}
.side.enduser{background:linear-gradient(180deg,#2554d6,#1b3fa8)}
.brand{padding:18px 20px;font-weight:700;font-size:17px;color:#fff;border-bottom:1px solid rgba(255,255,255,.12)}
.brand small{display:block;font-weight:500;font-size:11px;color:rgba(255,255,255,.7);margin-top:2px}
.nav a{display:block;padding:10px 20px;color:#e5e7eb;border-left:3px solid transparent}
.nav a:hover{background:rgba(255,255,255,.08);text-decoration:none}
.nav a.active{background:rgba(255,255,255,.14);border-left-color:#fff;color:#fff;font-weight:600}
.nav .grouphdr{padding:14px 20px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:rgba(255,255,255,.55)}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.top{background:var(--card);border-bottom:1px solid var(--line);padding:12px 26px;display:flex;align-items:center;justify-content:space-between}
.top .who{font-size:13px;color:var(--muted)}
.top .who b{color:#111}
.content{padding:26px;max-width:1050px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 10px}
.sub{color:var(--muted);margin:0 0 18px}
.cards{display:flex;flex-wrap:wrap;gap:14px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;min-width:210px;flex:1 1 210px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.card h3{margin:0 0 6px;font-size:15px}.card p{margin:0;color:var(--muted);font-size:13px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:0 0 18px}
table{width:100%;border-collapse:collapse;background:var(--card)}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}
th{background:#f8fafc;color:#374151;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
.b-ok{background:#dcfce7;color:#166534}.b-off{background:#f1f5f9;color:#64748b}
.b-warn{background:#fef3c7;color:#92400e}.b-role{background:#e0e7ff;color:#3730a3}
.b-err{background:#fee2e2;color:#991b1b}.b-pii{background:#fae8ff;color:#86198f}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;width:100%}
label{display:block;font-size:13px;color:#374151;margin:10px 0 4px;font-weight:600}
.btn{display:inline-block;background:var(--blue);color:#fff;border:0;border-radius:7px;padding:8px 15px;font-weight:600;cursor:pointer}
.btn:hover{background:var(--blue2);text-decoration:none}
.btn.sm{padding:5px 10px;font-size:13px}
.btn.ghost{background:#eef2ff;color:var(--blue2)}.btn.grey{background:#eef2f7;color:#334155}
.btn.danger{background:#fee2e2;color:#991b1b}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>*{flex:1 1 220px}
.flash{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;padding:10px 14px;border-radius:8px;margin:0 0 16px}
.flash.err{background:#fef2f2;border-color:#fecaca;color:#991b1b}
.banner{background:#eff6ff;border:1px solid #bfdbfe;color:#1e3a8a;padding:10px 14px;border-radius:8px;margin:0 0 16px;font-size:14px}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.right{text-align:right}.nowrap{white-space:nowrap}
.kv{display:grid;grid-template-columns:150px 1fr;gap:8px 14px}.kv div.k{color:var(--muted)}
.chatlog{background:var(--card);border:1px solid var(--line);border-radius:10px;height:52vh;overflow:auto;padding:16px}
.msg{margin:0 0 12px;max-width:80%}.msg .bub{padding:10px 14px;border-radius:12px;display:inline-block}
.msg.u{margin-left:auto;text-align:right}.msg.u .bub{background:var(--blue);color:#fff}
.msg.a .bub{background:#f1f5f9;color:#111}.msg .meta{font-size:11px;color:var(--muted);margin-top:3px}
.inbar{display:flex;gap:10px;margin-top:12px}.inbar input{flex:1}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def badge(text, kind="ok") -> str:
    return f'<span class="badge b-{kind}">{esc(text)}</span>'


def _nav(items, active):
    out = []
    for it in items:
        if it.get("hdr"):
            out.append(f'<div class="grouphdr">{esc(it["hdr"])}</div>')
            continue
        cls = "active" if it["href"] == active else ""
        out.append(f'<a class="{cls}" href="{esc(it["href"])}">{esc(it["label"])}</a>')
    return '<div class="nav">' + "".join(out) + "</div>"


def enduser_nav(role: str, active: str):
    items = [{"href": "/", "label": "Home"}, {"href": "/chat", "label": "Assistant"},
             {"href": "/me", "label": "My Profile"}]
    if role_rules.can(role, "my_team"):        # driven by the editable capability matrix
        items.append({"href": "/team", "label": "My Team"})
    items.append({"href": "/directory", "label": "Directory"})
    if role_rules.can(role, "reports_analytics"):
        items.append({"href": "/reports", "label": "Reports"})
    if role_rules.can(role, "approvals"):
        items.append({"href": "/approvals", "label": "Approvals"})
    items.append({"href": "/policies", "label": "Policies"})
    return _nav(items, active)


def admin_nav(active: str):
    items = [{"href": "/admin", "label": "Dashboard"},
             {"hdr": "Configure"},
             {"href": "/admin/sources", "label": "Data Sources"},
             {"href": "/admin/tools", "label": "MCP Tools"},
             {"href": "/admin/models", "label": "AI Models"},
             {"href": "/admin/roles", "label": "Roles & Access"},
             {"hdr": "Operate"},
             {"href": "/admin/users", "label": "Users & Sessions"},
             {"href": "/admin/cache", "label": "Cache"},
             {"href": "/admin/audit", "label": "Audit Log"},
             {"href": "/admin/settings", "label": "Settings"}]
    return _nav(items, active)


ROLE_LABEL = {"employee": "Employee", "manager": "Manager", "hr_admin": "HR Admin", "guest": "Guest"}


def doc(brand, title, nav_html, body, user=None, admin=False, flash="", flash_err=False, csrf=""):
    if csrf:   # inject an anti-CSRF token into every POST form
        _hidden = f'<input type="hidden" name="_csrf" value="{esc(csrf)}">'
        body = re.sub(r'(<form\b[^>]*method="post"[^>]*>)', lambda m: m.group(1) + _hidden, body, flags=re.I)
    side_cls = "side" if admin else "side enduser"
    sub = "Admin Console" if admin else "End-User App"
    who = ""
    if user:
        rl = ROLE_LABEL.get(user.get("role", ""), user.get("role", ""))
        logout = "/admin/logout" if admin else "/logout"
        who = (f'<span class="who"><b>{esc(user.get("display_name","")) }</b>'
               + (f' · {esc(rl)}' if not admin else " · Platform Admin")
               + f'</span> &nbsp; <a class="btn sm grey" href="{logout}">Sign out</a>')
    flashhtml = f'<div class="flash{" err" if flash_err else ""}">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · {esc(brand)}</title><style>{CSS}</style></head><body>
<div class="layout">
 <div class="{side_cls}">
   <div class="brand">{esc(brand)}<small>{sub}</small></div>
   {nav_html}
 </div>
 <div class="main">
   <div class="top"><div>{esc(title)}</div><div>{who}</div></div>
   <div class="content">{flashhtml}{body}</div>
 </div>
</div></body></html>"""


def login_page(brand, per_user=False, admin=False, error=""):
    if admin:
        pw = '<label>Admin password</label><input name="password" type="password" placeholder="required if one is set">'
    elif per_user:
        pw = ('<label>Oracle password</label><input name="password" type="password" '
              'placeholder="used only to sign in as you; not stored">')
    else:
        pw = ""
    title = "Admin Console — Sign in" if admin else f"{brand} — Sign in"
    action = "/admin/login" if admin else "/login"
    hint = ("Platform Admins only." if admin else
            ("Sign in with your Oracle HCM username." if per_user
             else "Enter your work email, name, or employee number."))
    guest = "" if admin else '<p class="muted" style="margin-top:14px">or <a href="/login?guest=1">continue as guest</a> (directory only)</p>'
    err = f'<div class="flash err">{esc(error)}</div>' if error else ""
    accent = "#1f2937" if admin else "#2554d6"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>{CSS}</style></head><body style="background:var(--bg)">
<div style="max-width:400px;margin:9vh auto;background:#fff;border:1px solid var(--line);border-radius:14px;padding:30px 28px;box-shadow:0 10px 30px rgba(0,0,0,.06)">
<div style="font-weight:700;font-size:20px;color:{accent};margin-bottom:2px">{esc(brand)}</div>
<div class="muted" style="margin-bottom:18px">{ 'Admin Console' if admin else 'HR Assistant' }</div>
{err}
<form method="post" action="{action}">
<label>{ 'Admin email' if admin else 'Work email / name / employee #' }</label>
<input name="identifier" autofocus placeholder="{ 'you@company.com' if admin else 'jane.doe@example.com' }">
{pw}
<button class="btn" style="margin-top:16px;width:100%">Sign in</button>
</form>
<p class="muted" style="margin-top:12px;font-size:13px">{hint}</p>
{guest}
</div></body></html>"""


def table(headers, rows):
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="muted">Nothing to show.</td></tr>'
    return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


def cards(items):
    c = ""
    for it in items:
        inner = f'<h3>{esc(it["title"])}</h3><p>{esc(it.get("desc",""))}</p>'
        if it.get("href"):
            c += f'<a class="card" href="{esc(it["href"])}" style="color:inherit">{inner}</a>'
        else:
            c += f'<div class="card">{inner}</div>'
    return f'<div class="cards">{c}</div>'


def kv(pairs):
    inner = "".join(f'<div class="k">{esc(k)}</div><div>{v}</div>' for k, v in pairs)
    return f'<div class="panel"><div class="kv">{inner}</div></div>'
