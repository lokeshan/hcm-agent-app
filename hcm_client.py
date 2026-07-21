"""Oracle HCM REST client — per-connector (takes a `conn` config dict).

conn keys: _id, base_url, auth ('basic'|'oauth'), username, password,
client_id, client_secret, token_url, scope. Caching keyed by connector id.
"""
from __future__ import annotations
from urllib.parse import quote
import httpx

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import hashlib
import re

import config
import cache

RES = "/hcmRestApi/resources/latest/workers"

# Escape a value for safe interpolation into an Oracle ADF REST `q` string literal
# (single quote is the string delimiter; double it, and drop control chars).
_QBAD = str.maketrans({"'": "''", "\\": "", "\n": " ", "\r": " ", "\t": " "})


def _qesc(v) -> str:
    return str(v if v is not None else "").translate(_QBAD)
TIMEOUT = httpx.Timeout(15.0, connect=6.0)


def _cache_on():
    return config.as_bool(config.load().get("cache_enabled", "true"))


def _ttl():
    return config.as_int(config.load().get("cache_ttl", "300"))


def _principal(conn) -> str:
    """Identity that scopes cached data: the per-user username (basic) or OAuth
    client id. Prevents one user being served another user's cached record (CR-03)."""
    p = conn.get("username") or conn.get("client_id") or ""
    return hashlib.sha256(p.encode("utf-8")).hexdigest()[:12] if p else "anon"


def _ck(conn, *parts):
    base = str(conn.get("_id", conn.get("base_url", "")))
    return "hcm:" + base + ":" + _principal(conn) + ":" + ":".join(str(p) for p in parts)


def _cget(key):
    return cache.get(key) if _cache_on() else None


def _cset(key, val):
    if _cache_on():
        cache.set(key, val, _ttl())


def _base(conn) -> str:
    b = (conn.get("base_url") or "").rstrip("/")
    if not b:
        raise RuntimeError("No base URL configured for this connector.")
    return b


async def _oauth_token(conn) -> str:
    ck = _ck(conn, "oauth_token")
    tok = cache.get(ck)
    if tok:
        return tok
    data = {"grant_type": "client_credentials"}
    if conn.get("scope"):
        data["scope"] = conn["scope"]
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(conn.get("token_url", ""), data=data,
                         auth=(conn.get("client_id", ""), conn.get("client_secret", "")))
        r.raise_for_status()
        j = r.json()
    tok = j["access_token"]
    cache.set(ck, tok, max(60, int(j.get("expires_in", 3600)) - 60))
    return tok


async def _auth(conn):
    if conn.get("auth") == "oauth":
        return {"Authorization": f"Bearer {await _oauth_token(conn)}"}, None
    return {}, (conn.get("username", ""), conn.get("password", ""))


async def _get(conn, url_or_path, params=None):
    url = url_or_path if url_or_path.startswith("http") else _base(conn) + url_or_path
    headers, auth = await _auth(conn)
    headers["Accept"] = "application/json"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
        r = await c.get(url, params=params, headers=headers, auth=auth)
        if 300 <= r.status_code < 400:
            raise RuntimeError(f"Redirected (HTTP {r.status_code}) — check credentials / auth type.")
        r.raise_for_status()
        return r.json()


def _self(item):
    return next((l.get("href") for l in (item.get("links") or []) if l.get("rel") == "self"), None)


async def test_connection(conn) -> dict:
    try:
        _base(conn)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        d = await _get(conn, RES, {"onlyData": "true", "limit": 1, "fields": "PersonNumber,DisplayName"})
        return {"ok": True, "status": 200, "count": d.get("count")}
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        return {"ok": False, "status": code, "error": f"HTTP {code}" + (" — wrong credentials or no API access." if code == 401 else "")}
    except httpx.ConnectError as e:
        return {"ok": False, "error": f"Cannot reach the pod URL ({e})."}
    except Exception as e:
        msg = str(e)
        if "CERT" in msg.upper() or "SSL" in msg.upper():
            msg = "SSL verification failed — likely a corporate proxy/CA. " + msg
        return {"ok": False, "error": f"{type(e).__name__}: {msg}"}


async def search_workers(conn, query: str, limit: int = 25):
    key = _ck(conn, "search", (query or "").lower(), limit)
    c = _cget(key)
    if c is not None:
        return c
    d = await _get(conn, RES, {"q": f"DisplayName LIKE '%{_qesc(query)}%'", "limit": limit, "fields": "PersonNumber,DisplayName"})
    out = [{"PersonId": w.get("PersonNumber"), "PersonNumber": w.get("PersonNumber"),
            "DisplayName": w.get("DisplayName"), "JobTitle": None, "DepartmentName": None, "_href": _self(w)}
           for w in d.get("items", [])]
    _cset(key, out)
    return out


async def _resolve(conn, person_id):
    pid = str(person_id if person_id is not None else "").strip()
    if not pid:
        return None, None, None
    if pid.isdigit():   # only build the numeric filter for a genuine employee number
        try:
            d = await _get(conn, RES, {"q": f"PersonNumber={pid}", "limit": 1})
            it = (d.get("items") or [None])[0]
            if it:
                return _self(it), it.get("DisplayName"), it.get("PersonNumber")
        except Exception:
            pass
    s = await search_workers(conn, pid, limit=1)   # search_workers escapes the value
    if s:
        return s[0].get("_href"), s[0].get("DisplayName"), s[0].get("PersonNumber")
    return None, None, None


async def _primary_email(conn, href):
    if not href:
        return None
    try:
        d = await _get(conn, href + "/child/emails", {"onlyData": "true", "limit": 10})
        items = d.get("items", [])
        pick = next((e for e in items if str(e.get("PrimaryFlag")).lower() == "true"), None) or (items[0] if items else None)
        return pick.get("EmailAddress") if pick else None
    except Exception:
        return None


def _mgr(a: dict, out: dict):
    out["ManagerName"] = a.get("ManagerName") or a.get("LineManagerName") or out.get("ManagerName")
    out["ManagerId"] = (a.get("ManagerPersonNumber") or a.get("ManagerPersonId")
                        or a.get("ManagerId") or out.get("ManagerId"))


async def _assignment(conn, href) -> dict:
    out = {"JobTitle": None, "DepartmentName": None, "LocationName": None, "AssignmentStatus": None,
           "ManagerName": None, "ManagerId": None}
    if not href:
        return out
    try:
        d = await _get(conn, href + "/child/assignments", {"onlyData": "true", "limit": 1})
        a = (d.get("items") or [None])[0]
        if a:
            out["JobTitle"] = a.get("AssignmentName") or a.get("JobName") or a.get("PositionName")
            out["DepartmentName"] = a.get("DepartmentName")
            out["LocationName"] = a.get("LocationName") or a.get("LocationCode")
            out["AssignmentStatus"] = a.get("AssignmentStatusType") or a.get("AssignmentStatus")
            _mgr(a, out)
            return out
    except Exception:
        pass
    try:
        d = await _get(conn, href, {"onlyData": "true", "expand": "workRelationships.assignments"})
        wr = ((d.get("workRelationships") or {}).get("items") or [None])[0]
        a = (((wr or {}).get("assignments") or {}).get("items") or [None])[0]
        if a:
            out["JobTitle"] = a.get("AssignmentName") or a.get("JobName")
            out["DepartmentName"] = a.get("DepartmentName")
            out["LocationName"] = a.get("LocationName")
            _mgr(a, out)
    except Exception:
        pass
    return out


async def get_worker(conn, person_id: str):
    key = _ck(conn, "worker", person_id)
    c = _cget(key)
    if c is not None:
        return c
    href, disp, pnum = await _resolve(conn, person_id)
    if not disp:
        return {"error": "not_found"}
    w = {"DisplayName": disp, "PersonNumber": pnum, "JobTitle": None, "DepartmentName": None,
         "LocationName": None, "ManagerName": None, "WorkEmail": None, "WorkPhoneNumber": None,
         "HireDate": None, "AssignmentStatus": None}
    w["WorkEmail"] = await _primary_email(conn, href)
    w.update({k: v for k, v in (await _assignment(conn, href)).items() if v is not None})
    _cset(key, w)
    return w


async def get_assignment(conn, person_id: str):
    href, disp, pnum = await _resolve(conn, person_id)
    if not disp:
        return {"error": "not_found"}
    a = await _assignment(conn, href)
    a["PersonId"] = pnum
    return a


async def get_direct_reports(conn, person_id: str):
    key = _ck(conn, "reports", person_id)
    c = _cget(key)
    if c is not None:
        return c
    href, disp, pnum = await _resolve(conn, person_id)
    out = []
    try:
        d = await _get(conn, href + "/child/directReports", {"onlyData": "true", "limit": 50})
        out = [{"PersonId": w.get("PersonNumber"), "PersonNumber": w.get("PersonNumber"),
                "DisplayName": w.get("DisplayName"), "JobTitle": None, "DepartmentName": None}
               for w in d.get("items", [])]
    except Exception:
        out = []
    _cset(key, out)
    return out


async def list_by_department(conn, department: str, limit: int = 100):
    """Best-effort live department listing via the ADF assignments finder.
    Pods that don't expose this finder return an empty list (agent reports none)."""
    dept = _qesc(department or "")
    if not dept:
        return []
    key = _ck(conn, "dept", dept.lower(), limit)
    cached = _cget(key)
    if cached is not None:
        return cached
    out = []
    for q in (f"assignments.DepartmentName='{dept}'", f"assignments.DepartmentName LIKE '%{dept}%'"):
        try:
            d = await _get(conn, RES, {"q": q, "onlyData": "true", "limit": limit,
                                       "fields": "PersonNumber,DisplayName"})
            items = d.get("items", [])
            if items:
                out = [{"PersonId": w.get("PersonNumber"), "PersonNumber": w.get("PersonNumber"),
                        "DisplayName": w.get("DisplayName"), "JobTitle": None,
                        "DepartmentName": department} for w in items]
                break
        except Exception:
            continue
    _cset(key, out)
    return out


async def get_management_chain(conn, person_id: str, max_depth: int = 8):
    """Walk the reporting line upward using each assignment's manager reference
    (pod-dependent field names; degrades gracefully to what it can resolve)."""
    chain, seen = [], set()
    href, _, _ = await _resolve(conn, person_id)
    for _ in range(max_depth):
        if not href:
            break
        a = await _assignment(conn, href)
        mname, mid = a.get("ManagerName"), a.get("ManagerId")
        if not (mname or mid):
            break
        key = str(mid or mname)
        if key in seen:
            break
        seen.add(key)
        chain.append({"PersonId": mid, "DisplayName": mname, "JobTitle": None})
        if not mid:
            break
        href, _, _ = await _resolve(conn, mid)
    return chain


async def overview(conn) -> dict:
    """Best-effort headcount for the Reports page (department breakdown needs an
    HR extract on a real pod; here we return what the account is allowed to count)."""
    try:
        d = await _get(conn, RES, {"onlyData": "true", "limit": 200, "fields": "PersonNumber,DisplayName"})
        total = d.get("count") or len(d.get("items", []))
        return {"total": total, "by_department": [], "spans": [],
                "note": "Live department headcount needs an HR extract; showing the count this account can see."}
    except Exception as e:
        return {"total": 0, "by_department": [], "spans": [], "note": f"Unavailable: {e}"}


def _apply_map(item: dict, rmap: dict) -> dict:
    if rmap:
        return {out: item.get(inp) for out, inp in rmap.items()}
    return {k: v for k, v in item.items() if k != "links" and not str(k).startswith("_")}


async def run_custom(conn, cfg: dict, args: dict):
    """Generic executor for CONFIG tools (kind + endpoint + params + result_map).
    Lets admins add new Oracle tools from the UI with no code."""
    kind = cfg.get("kind"); p = dict(cfg.get("params") or {}); rmap = cfg.get("result_map") or {}
    ttl = int(cfg.get("cache_ttl") or 0)
    arg = cfg.get("arg", "person_id")
    argval = args.get(arg, args.get("person_id", args.get("query")))
    ck = _ck(conn, "custom", cfg.get("name", ""), str(argval))
    if ttl and _cache_on():
        cached = cache.get(ck)
        if cached is not None:
            return cached
    try:
        if kind == "oracle_search":
            attr = re.sub(r"[^A-Za-z0-9_.]", "", str(p.get("attr", "DisplayName"))) or "DisplayName"
            op = str(p.get("op", "LIKE")).upper()
            op = op if op in ("LIKE", "=") else "LIKE"
            val = _qesc(args.get("query", argval or ""))
            q = f"{attr} LIKE '%{val}%'" if op == "LIKE" else f"{attr}='{val}'"
            d = await _get(conn, RES, {"q": q, "limit": p.get("limit", 25),
                                       "fields": p.get("fields", "PersonNumber,DisplayName")})
            out = [_apply_map(w, rmap) for w in d.get("items", [])]
        elif kind == "oracle_child":
            href, _, _ = await _resolve(conn, argval)
            if not href:
                out = []
            else:
                gp = {"onlyData": "true", "limit": p.get("limit", 50)}
                if p.get("fields"):
                    gp["fields"] = p["fields"]
                child = re.sub(r"[^A-Za-z0-9_]", "", cfg.get("endpoint") or "")  # no path traversal
                d = await _get(conn, href + "/child/" + child, gp)
                out = [_apply_map(w, rmap) for w in d.get("items", [])]
        elif kind == "oracle_detail":
            href, _, _ = await _resolve(conn, argval)
            if not href:
                out = {"error": "not_found"}
            else:
                gp = {"onlyData": "true"}
                if p.get("expand"):
                    gp["expand"] = p["expand"]
                d = await _get(conn, href, gp)
                out = _apply_map(d, rmap)
        else:
            out = {"error": "unsupported_kind", "kind": kind}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if ttl and _cache_on():
        cache.set(ck, out, ttl)
    return out


async def role_signals(conn, person_id: str) -> dict:
    """Infer role from what THIS user's Oracle account returns (Oracle enforces
    security; we just observe the breadth of what comes back):
      - report_count            : # of direct reports  -> Manager if > 0
      - secured_beyond_reports  : can read SECURED data (assignment/dept) for a
                                  worker who is neither self nor a report -> HR-level
    Note: calibrate against your pod's data-security config. If your org exposes a
    broad public directory with assignment data to everyone, raise
    `hr_visibility_threshold` or rely on the hr_admin_emails override.
    """
    reports = await get_direct_reports(conn, person_id)
    rc = len(reports)
    report_ids = {str(r.get("PersonNumber")) for r in reports}
    threshold = config.as_int(config.load().get("hr_visibility_threshold", "1"), 1)
    others_seen, secured = 0, 0
    try:
        d = await _get(conn, RES, {"onlyData": "true", "limit": 50,
                                   "fields": "PersonNumber,DisplayName"})
        non_reports = [str(w.get("PersonNumber")) for w in d.get("items", [])
                       if w.get("PersonNumber")
                       and str(w.get("PersonNumber")) != str(person_id)
                       and str(w.get("PersonNumber")) not in report_ids]
        others_seen = len(non_reports)
        # Probe a few non-reports for SECURED assignment data; that is the real
        # "sees more than reportees" signal, not just a directory name.
        for pn in non_reports[:5]:
            a = await get_assignment(conn, pn)
            if a and not a.get("error") and (a.get("JobTitle") or a.get("DepartmentName")):
                secured += 1
            if secured >= threshold:
                break
    except Exception:
        pass
    return {"report_count": rc, "others_seen": others_seen,
            "secured_beyond_reports": secured >= threshold}
