"""Oracle HCM MCP server (FastMCP) — governed + audited tool layer.

Each tool:
  1. routes through the connector registry (sources.py) to the primary connector,
  2. is checked against the tool registry (enabled + allowed_roles for the current
     user's role) — governance only, since Oracle enforces the real data security,
  3. is timed and written to the audit log.
"""
from __future__ import annotations
import json
import time
import threading

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from pydantic import ConfigDict

import sources
import identity
import tools_registry as registry
import audit_store
import connectors

mcp = FastMCP("oracle-hcm")
_registered: dict = {}   # name -> (arg, description) currently registered as dynamic tools
_sync_lock = threading.Lock()   # serialize mutations of the shared MCP server


def _clean(items):
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in (items or [])]


def _source_name() -> str:
    p = connectors.primary()
    return p["name"] if p else "?"


async def _run(name: str, coro, target_ref: str = ""):
    """Governance + audit wrapper shared by every tool."""
    u = identity.get_current()
    role = u.get("role", "employee")
    who = u.get("email") or u.get("display_name") or "anonymous"
    allowed, reason = registry.check(name, role)
    if not allowed:
        # callers build the coroutine eagerly (_run(name, sources.x(...))), so on the
        # deny path it is never awaited — close it or every denial leaks a coroutine
        # and emits "coroutine was never awaited".
        coro.close()
        audit_store.log(who, role, name, _source_name(), target_ref, f"denied:{reason}", 0)
        return registry.deny_value(name)
    t0 = time.time()
    try:
        result = await coro
        audit_store.log(who, role, name, _source_name(), target_ref, "ok",
                        int((time.time() - t0) * 1000))
        return result
    except Exception as e:
        audit_store.log(who, role, name, _source_name(), target_ref,
                        f"error:{type(e).__name__}", int((time.time() - t0) * 1000))
        raise


@mcp.tool
async def search_workers(query: str) -> list:
    """Find workers by (partial) name, email, department, or job title. Call first to
    resolve a person before fetching details. Returns lightweight matches."""
    return _clean(await _run("search_workers", sources.search(query), query))


@mcp.tool
async def get_worker(person_id: str) -> dict:
    """Get a worker's profile (name, job, department, location, manager, email, phone, status)."""
    return await _run("get_worker", sources.get_worker(person_id), person_id)


@mcp.tool
async def get_assignment(person_id: str) -> dict:
    """Get a worker's assignment: job, department, location, manager, status."""
    return await _run("get_assignment", sources.get_assignment(person_id), person_id)


@mcp.tool
async def get_direct_reports(person_id: str) -> list:
    """List the direct reports of a manager (by PersonId / employee number)."""
    return _clean(await _run("get_direct_reports", sources.get_direct_reports(person_id), person_id))


@mcp.tool
async def list_by_department(department: str) -> list:
    """List workers in a department."""
    return _clean(await _run("list_by_department", sources.list_by_department(department), department))


@mcp.tool
async def get_management_chain(person_id: str) -> list:
    """The management chain above a worker, immediate manager first up to the top."""
    return _clean(await _run("get_management_chain", sources.get_management_chain(person_id), person_id))


class _ConfigTool(Tool):
    """A UI-defined tool. fastmcp normally derives a tool's schema from a Python
    function signature, which would cap us at the argument names we hard-coded;
    supplying `parameters` directly is the supported alternative (fastmcp's own
    TransformedTool does the same) and is what lets an admin declare any number
    of arguments. Governance + audit still run through the shared _run wrapper."""
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    async def run(self, arguments: dict) -> ToolResult:
        args = {k: v for k, v in (arguments or {}).items() if v is not None}
        ref = ", ".join(f"{k}={v}" for k, v in sorted(args.items()))[:200]
        out = await _run(self.name, sources.run_custom(self.name, **args), ref)
        return ToolResult(out if out is not None else {})


def _remove(n):
    try:
        mcp.local_provider.remove_tool(n)
    except Exception:
        try:
            mcp.remove_tool(n)
        except Exception:
            pass


def sync_custom_tools():
    """Register/refresh/remove UI-defined CONFIG tools on the live MCP server so the
    agent actually sees tools that admins add — no restart, no code. Serialized by a
    lock so concurrent requests don't mutate the shared server mid-run."""
    with _sync_lock:
        want = {t["name"]: t for t in registry.custom_enabled()}
        for n in list(_registered):
            if n not in want:
                _remove(n)
                _registered.pop(n, None)
        for n, cfg in want.items():
            sig = (json.dumps(registry.json_schema(cfg), sort_keys=True), cfg["description"])
            if _registered.get(n) == sig:
                continue
            if n in _registered:
                _remove(n)
            try:
                mcp.add_tool(_ConfigTool(name=n, description=cfg["description"] or n,
                                         parameters=registry.json_schema(cfg)))
                _registered[n] = sig
            except Exception as e:
                print("[mcp] could not register custom tool", n, ":", e)


if __name__ == "__main__":
    sync_custom_tools()
    mcp.run(show_banner=False)
