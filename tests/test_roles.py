"""Data-driven role detection + per-user auth merge (mock data, offline).
Run:  python tests/test_roles.py"""
import asyncio, sys, os, tempfile
from pathlib import Path

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import config
config.DB_PATH = Path(tempfile.gettempdir()) / "hcm_roles.db"
if config.DB_PATH.exists():
    config.DB_PATH.unlink()
import connectors, sources, identity, mock_data, session_ctx
connectors.list_all(); connectors.set_primary("mock")

P = F = 0
def check(label, got, want):
    global P, F
    good = got == want
    P, _ = (P + 1, None) if good else (P, None)
    if not good: F += 1
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r} want={want!r}")

async def main():
    print("== role inferred from returned data ==")
    expect = {"Jane Doe": "employee", "Michael Chen": "manager", "Priya Nair": "manager",
              "Robert King": "manager", "Sara Williams": "hr_admin", "Linda Gomez": "hr_admin",
              "David Smith": "employee", "Emily Turner": "manager", "Ahmed Khan": "employee"}
    for w in mock_data.WORKERS:
        sig = mock_data.role_signals(w["PersonId"])
        role = identity._classify(w["WorkEmail"], w["PersonNumber"], w["WorkEmail"].lower(), sig)
        check(w["DisplayName"], role, expect[w["DisplayName"]])

    print("\n== whoami end-to-end ==")
    check("Jane -> employee", (await identity.whoami("jane.doe@example.com"))["role"], "employee")
    check("Michael -> manager", (await identity.whoami("michael.chen@example.com"))["role"], "manager")
    check("Sara -> hr_admin", (await identity.whoami("sara.williams@example.com"))["role"], "hr_admin")
    check("emp# 100003 -> manager", (await identity.whoami("100003"))["role"], "manager")

    print("\n== hr_admin_emails override ==")
    config.save({"hr_admin_emails": "jane.doe@example.com"})
    check("Jane allow-listed -> hr_admin", (await identity.whoami("jane.doe@example.com"))["role"], "hr_admin")
    config.save({"hr_admin_emails": ""})

    print("\n== per-user auth overrides service account ==")
    fake = {"id": "orc", "type": "oracle_hcm", "config": {"username": "svc", "password": "svc"}}
    session_ctx.set_auth({})
    check("no session -> service account", sources._cfg(fake)["username"], "svc")
    session_ctx.set_auth({"username": "alice@corp.com", "password": "hers"})
    check("session -> user creds", sources._cfg(fake)["username"], "alice@corp.com")
    session_ctx.set_auth({})

    print(f"\n===== {P} passed, {F} failed =====")
    sys.exit(1 if F else 0)

asyncio.run(main())
