"""Audit log (§7 A8 / §10) — every tool access recorded for compliance.

audit(ts, user, role, tool, source, target_ref, result, latency_ms)

Note: Oracle HCM is the security boundary (it decides what data each user may
see). This log is an *observability / compliance* record of which tool ran for
whom — not an access-control gate.
"""
from __future__ import annotations
import sqlite3, time, io, csv

import config


def _conn():
    c = config.tune(sqlite3.connect(config.DB_PATH, timeout=5))
    c.execute("""CREATE TABLE IF NOT EXISTS audit
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, role TEXT,
                  tool TEXT, source TEXT, target_ref TEXT, result TEXT, latency_ms INTEGER)""")
    return c


def log(user: str, role: str, tool: str, source: str,
        target_ref: str = "", result: str = "ok", latency_ms: int = 0) -> None:
    try:
        c = _conn()
        c.execute("INSERT INTO audit(ts,user,role,tool,source,target_ref,result,latency_ms) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (time.time(), user or "?", role or "?", tool, source or "",
                   str(target_ref or ""), result, int(latency_ms)))
        c.commit(); c.close()
    except Exception:
        pass  # auditing must never break a request


def list_recent(limit: int = 200, tool: str = "", role: str = "", result: str = "") -> list:
    c = _conn()
    sql = "SELECT ts,user,role,tool,source,target_ref,result,latency_ms FROM audit"
    where, args = [], []
    if tool:
        where.append("tool=?"); args.append(tool)
    if role:
        where.append("role=?"); args.append(role)
    if result:
        where.append("result=?"); args.append(result)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"; args.append(int(limit))
    rows = c.execute(sql, args).fetchall(); c.close()
    out = []
    for r in rows:
        out.append({"ts": r[0], "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0])),
                    "user": r[1], "role": r[2], "tool": r[3], "source": r[4],
                    "target_ref": r[5], "result": r[6], "latency_ms": r[7]})
    return out


def stats() -> dict:
    c = _conn()
    total = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    errors = c.execute("SELECT COUNT(*) FROM audit WHERE result!='ok'").fetchone()[0]
    today0 = time.time() - 86400
    today = c.execute("SELECT COUNT(*) FROM audit WHERE ts>=?", (today0,)).fetchone()[0]
    avg = c.execute("SELECT AVG(latency_ms) FROM audit WHERE ts>=?", (today0,)).fetchone()[0]
    recent_err = [{"when": time.strftime("%H:%M:%S", time.localtime(r[0])), "tool": r[1],
                   "result": r[2], "source": r[3]}
                  for r in c.execute("SELECT ts,tool,result,source FROM audit WHERE result!='ok' "
                                     "ORDER BY ts DESC LIMIT 5")]
    c.close()
    return {"total": total, "errors": errors, "today": today,
            "avg_latency_ms": int(avg or 0), "recent_errors": recent_err}


def _csv_safe(v):
    """Neutralize spreadsheet formula injection: prefix risky leading chars with a quote."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["when", "user", "role", "tool", "source", "target_ref", "result", "latency_ms"])
    for r in list_recent(limit=100000):
        w.writerow([_csv_safe(r["when"]), _csv_safe(r["user"]), _csv_safe(r["role"]), _csv_safe(r["tool"]),
                    _csv_safe(r["source"]), _csv_safe(r["target_ref"]), _csv_safe(r["result"]), r["latency_ms"]])
    return buf.getvalue()
