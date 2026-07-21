"""Unit tests for the security/robustness fixes (no server needed).
Run:  python tests/test_security.py"""
import sys, os, tempfile
from pathlib import Path

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import config
config.DB_PATH = Path(tempfile.gettempdir()) / "hcm_sec.db"
if config.DB_PATH.exists():
    config.DB_PATH.unlink()

import hcm_client, tools_registry as reg, cache, connectors
connectors.list_all()

P = F = 0
def ok(l, c, x=""):
    global P, F
    if c: P += 1; print("  PASS", l)
    else: F += 1; print("  FAIL", l, x)

print("== Oracle q escaping (CR-04) ==")
ok("single quote doubled", hcm_client._qesc("O'Brien") == "O''Brien")
ok("no lone quote survives", "'" not in hcm_client._qesc("x' or 1=1 --").replace("''", ""))
ok("control chars stripped", "\n" not in hcm_client._qesc("a\nb") and "\\" not in hcm_client._qesc("a\\b"))

print("== identity-scoped cache key (CR-03) ==")
a = {"_id": "c1", "username": "alice@corp"}
b = {"_id": "c1", "username": "bob@corp"}
ok("different users -> different keys", hcm_client._ck(a, "worker", "100") != hcm_client._ck(b, "worker", "100"))
ok("same user -> same key", hcm_client._ck(a, "worker", "100") == hcm_client._ck(dict(a), "worker", "100"))
ok("no principal -> anon", hcm_client._principal({}) == "anon")

print("== governance fail-closed (CR / B6) ==")
ok("unknown tool denied", reg.check("does_not_exist", "employee") == (False, "unknown_tool"))
ok("known allowed tool ok", reg.check("search_workers", "employee")[0] is True)
ok("disabled builtin denied", reg.check("get_compensation", "hr_admin")[0] is False)  # seeded disabled

print("== delete() truthful status (Q30b) ==")
ok("delete missing -> False", reg.delete("never_existed") is False)
ok("delete builtin -> False", reg.delete("get_worker") is False and reg.get("get_worker") is not None)

print("== bounded cache eviction (B9) ==")
_orig = cache.MAX_ENTRIES
cache.MAX_ENTRIES = 10
cache.clear()
for i in range(60):
    cache.set(f"k{i}", i, 60)
ok("cache size bounded by MAX_ENTRIES", len(cache._store) <= 10, len(cache._store))
cache.MAX_ENTRIES = _orig

print(f"\n===== {P} passed, {F} failed =====")
sys.exit(1 if F else 0)
