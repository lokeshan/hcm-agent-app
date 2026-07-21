import sys, tempfile, sqlite3
from pathlib import Path
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
config.DB_PATH = Path(tempfile.gettempdir()) / "hcm_migr.db"
if config.DB_PATH.exists(): config.DB_PATH.unlink()
# simulate an OLD (pre-enhancement) tools table, like the user's real DB
c = sqlite3.connect(config.DB_PATH)
c.execute("""CREATE TABLE tools (name TEXT PRIMARY KEY, source_type TEXT, enabled INTEGER,
   allowed_roles TEXT, cache_ttl INTEGER, pii_level TEXT, namespaced INTEGER, description TEXT, rest_mapping TEXT)""")
c.execute("INSERT INTO tools VALUES('get_worker','oracle_hcm',1,'employee,manager,hr_admin',300,'medium',0,'Full profile','GET ...')")
c.commit(); c.close()

import tools_registry as reg
P=F=0
def ok(l,cond,x=""):
    global P,F
    P,_=(P+1,0) if cond else (P,0)
    if not cond: F+=1
    print(("  PASS " if cond else "  FAIL ")+l+("" if cond else f"  {x}"))

reg.list_all()  # triggers migration (adds TEXT columns)
reg.upsert({"name":"list_absences","kind":"oracle_child","endpoint":"absences","enabled":1,
            "allowed_roles":["employee","manager"],"arg":"person_id","source_type":"oracle_hcm"})
t = reg.get("list_absences")
ok("config tool NOT builtin on migrated DB", t["builtin"] is False, t)
ok("kind preserved", t["kind"]=="oracle_child", t)
ok("in custom_enabled (would be MCP-registered)", "list_absences" in [x["name"] for x in reg.custom_enabled()])
ok("builtin name stays builtin", reg.get("get_worker")["builtin"] is True)
ok("config tool deletable", reg.delete("list_absences") is True and reg.get("list_absences") is None)
ok("builtin not deletable", reg.delete("get_worker") is False)
print(f"\n===== {P} passed, {F} failed =====")
sys.exit(1 if F else 0)
