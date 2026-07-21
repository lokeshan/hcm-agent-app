"""Mount third-party MCP servers as governed agent toolsets (§8.3 external_mcp).

An admin adds an `external_mcp` connector; its remote tools are namespaced
`<prefix>_<remote_name>` and only reach the model if the tool registry holds a
row under that prefixed name, enabled, with the caller's role in allowed_roles.
Absent from the registry means denied — mounting a server grants nothing.

Enforced at both layers, exactly like the local tools: `.filtered()` so the model
never sees a disallowed tool, and `process_tool_call` (the mirror of
hcm_mcp_server._run) so a call that slips through is still refused and audited.

MCPToolset instances are cached at module scope: the app builds a fresh Agent per
chat message and the Agent tears the MCP session down at refcount 0, so without
the cache every message would pay a full connect + initialize + tools/list.
"""
from __future__ import annotations
import hashlib, json, re, time
from dataclasses import dataclass
from typing import Any

from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets.wrapper import WrapperToolset

import audit_store
import connectors
import identity
import tools_registry as registry

TYPE = "external_mcp"
_INIT_TIMEOUT = 10.0                              # fail fast; a hung server must not stall a turn
_cache: dict[str, tuple[str, MCPToolset]] = {}    # connector id -> (config fingerprint, toolset)


# --- connector -> toolset ------------------------------------------------------

def enabled_sources() -> list[dict]:
    return [c for c in connectors.enabled_list() if c["type"] == TYPE]


def prefix_for(c: dict) -> str:
    """Tool-name namespace. Sanitised because pydantic-ai joins it with '_' into a
    tool name the model must be able to emit; falls back to the connector id."""
    p = re.sub(r"[^a-z0-9_]+", "_", (c.get("config", {}).get("mcp_prefix") or "").lower()).strip("_")
    return p or "mcp_" + str(c.get("id") or "ext")


def _fingerprint(cfg: dict) -> str:
    # hashed, never stored raw — a token must not sit in a module-level dict
    raw = json.dumps([cfg.get(k, "") for k in ("mcp_url", "mcp_auth", "mcp_token", "mcp_prefix")])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _auth(cfg: dict):
    kind = (cfg.get("mcp_auth") or "none").lower()
    if kind == "oauth":
        return "oauth"
    tok = (cfg.get("mcp_token") or "").strip()
    return tok if (kind == "bearer" and tok) else None   # pydantic-ai treats a str as a bearer token


def _governor(prefix: str, source_name: str):
    """Call-time gate + audit for one remote server — the remote twin of
    hcm_mcp_server._run. `name` arrives unprefixed (PrefixedToolset strips it
    before delegating), so re-namespace it to look up the registry."""
    async def process(ctx, call_tool, name: str, args: dict[str, Any]):
        tool = f"{prefix}_{name}"
        u = identity.get_current()
        role = u.get("role", "employee")
        who = u.get("email") or u.get("display_name") or "anonymous"
        ref = json.dumps(args, default=str)[:200]
        allowed, reason = registry.check(tool, role)
        if not allowed:
            audit_store.log(who, role, tool, source_name, ref, f"denied:{reason}", 0)
            return registry.deny_value(tool)
        t0 = time.time()
        try:
            result = await call_tool(name, args)
            audit_store.log(who, role, tool, source_name, ref, "ok", int((time.time() - t0) * 1000))
            return result
        except Exception as e:
            audit_store.log(who, role, tool, source_name, ref, f"error:{type(e).__name__}",
                            int((time.time() - t0) * 1000))
            raise
    return process


def _new(c: dict) -> MCPToolset:
    """Fresh (uncached) toolset for a connector dict. Connection is lazy."""
    cfg = c.get("config") or {}
    url = (cfg.get("mcp_url") or "").strip()
    if not url:
        raise ValueError("MCP server URL is required")
    return MCPToolset(url, auth=_auth(cfg), init_timeout=_INIT_TIMEOUT,
                      id=f"ext:{c.get('id') or 'new'}",
                      process_tool_call=_governor(prefix_for(c), c.get("name") or TYPE))


def _cached(c: dict) -> MCPToolset:
    fp = _fingerprint(c.get("config") or {})
    hit = _cache.get(c["id"])
    if hit and hit[0] == fp:
        return hit[1]
    ts = _new(c)
    _cache[c["id"]] = (fp, ts)
    return ts


def invalidate(cid: str | None = None) -> None:
    """Drop cached toolsets after a connector edit/delete."""
    if cid:
        _cache.pop(cid, None)
    else:
        _cache.clear()


@dataclass
class _Resilient(WrapperToolset):
    """A dead external server must never break chat: swallow connect/list failures
    and contribute no tools.

    `depth` counts only the enters that actually succeeded, so each one is paired
    with exactly one delegated exit. It must be a counter, not a flag: the Agent
    enters a toolset once for its own context and again per run, and MCPToolset's
    __aexit__ raises if called more often than __aenter__ — while swallowing a
    surplus exit would leave the MCP session open forever.
    """
    depth: int = 0

    async def __aenter__(self):
        try:
            await self.wrapped.__aenter__()
            self.depth += 1
        except Exception as e:
            print("[mcp] external server unavailable:", type(e).__name__, e)
        return self

    async def __aexit__(self, *args):
        if self.depth <= 0:
            return None
        self.depth -= 1
        try:
            return await self.wrapped.__aexit__(*args)
        except Exception:
            return None

    async def get_tools(self, ctx):
        if self.depth <= 0:
            return {}
        try:
            return await self.wrapped.get_tools(ctx)
        except Exception as e:
            print("[mcp] could not list external tools:", type(e).__name__, e)
            return {}


def toolsets_for_agent(role: str | None) -> list:
    """Governed, prefixed, cached toolsets for every enabled external_mcp connector.
    Sync on purpose — build_agent is sync and MCPToolset connects lazily."""
    srcs = enabled_sources()
    if not srcs:
        return []                                   # common case: zero cost, no network
    # Always role-filtered, demo mode included: the scripted demo brain knows nothing
    # about external servers, so agent.py's demo exemption must not apply here.
    allowed = registry.allowed_names_for_role(role) if role else set()
    out, seen = [], set()
    for c in srcs:
        p = prefix_for(c)
        if p in seen:                               # same prefix twice => tool-name clash => Agent error
            print("[mcp] skipping", c["name"], "- duplicate tool prefix", p)
            continue
        try:
            ts = _cached(c)
        except Exception as e:
            print("[mcp] cannot mount", c["name"], ":", e)
            continue
        seen.add(p)
        out.append(_Resilient(ts.prefixed(p).filtered(lambda ctx, td: td.name in allowed)))
    return out


# --- admin console API ---------------------------------------------------------

async def discover(connector: dict) -> dict:
    """Preview a server's tools. Never raises — returns {"ok", "tools", "error"}.
    Each tool carries its prefixed name and whether the registry already allows it."""
    try:
        ts = _new(connector)
    except Exception as e:
        return {"ok": False, "tools": [], "prefix": "", "error": str(e)}
    p = prefix_for(connector)
    try:
        tools = await ts.list_tools()               # self-enters and tears down again
    except Exception as e:
        return {"ok": False, "tools": [], "prefix": p, "error": f"{type(e).__name__}: {e}"}
    known = {t["name"] for t in registry.list_all()}
    return {"ok": True, "error": "", "prefix": p,
            "tools": [{"name": f"{p}_{t.name}", "remote_name": t.name,
                       "description": t.description or "", "schema": t.inputSchema or {},
                       "registered": f"{p}_{t.name}" in known} for t in tools]}


async def test(connector: dict) -> dict:
    d = await discover(connector)
    return {"ok": True, "status": 200, "count": len(d["tools"])} if d["ok"] \
        else {"ok": False, "error": d["error"]}


if __name__ == "__main__":   # self-check: governance is fail-closed. No network needed.
    import asyncio
    assert prefix_for({"id": "abc", "config": {"mcp_prefix": "Work Day!"}}) == "work_day"
    assert prefix_for({"id": "abc", "config": {}}) == "mcp_abc"
    assert _auth({"mcp_auth": "none", "mcp_token": "t"}) is None
    assert _auth({"mcp_auth": "bearer", "mcp_token": "t"}) == "t"
    reached = []

    async def _server(name, args, **kw):
        reached.append(name)
        return {"ok": 1}

    # a tool with no registry row must never reach the server
    out = asyncio.run(_governor("selfchk", "test")(None, _server, "get_worker", {"id": 1}))
    assert reached == [] and out.get("error") == "not_permitted", out
    print("mcp_sources self-check OK")
