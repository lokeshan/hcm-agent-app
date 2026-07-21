"""Central dispatch — routes tool calls to the PRIMARY enabled connector.

Keeps the agent, MCP tools and identity code source-agnostic: they call sources.*
and this module picks mock vs oracle_hcm (per the connector registry).
"""
from __future__ import annotations
import connectors
import mock_data
import hcm_client
import session_ctx
import tools_registry


def _primary():
    return connectors.primary()


def _cfg(c):
    """Connector config for a call. For live Oracle sources, the signed-in user's
    own credentials (session auth override) take precedence over the connector's
    service account — so the call runs AS that user and Oracle enforces security."""
    cfg = {**c["config"], "_id": c["id"]}
    if c["type"] == "oracle_hcm":
        auth = session_ctx.get_auth()
        if auth:
            cfg.update({k: v for k, v in auth.items() if v not in (None, "")})
    return cfg


async def search(query):
    c = _primary()
    if not c:
        return []
    if c["type"] == "mock":
        return mock_data.search_workers(query)
    return await hcm_client.search_workers(_cfg(c), query)


async def get_worker(person_id):
    c = _primary()
    if not c:
        return {"error": "no_source"}
    if c["type"] == "mock":
        return mock_data.get_worker(person_id)
    return await hcm_client.get_worker(_cfg(c), person_id)


async def get_assignment(person_id):
    c = _primary()
    if not c:
        return {"error": "no_source"}
    if c["type"] == "mock":
        return mock_data.get_assignment(person_id)
    return await hcm_client.get_assignment(_cfg(c), person_id)


async def get_direct_reports(person_id):
    c = _primary()
    if not c:
        return []
    if c["type"] == "mock":
        return mock_data.get_direct_reports(person_id)
    return await hcm_client.get_direct_reports(_cfg(c), person_id)


async def list_by_department(department):
    c = _primary()
    if not c:
        return []
    if c["type"] == "mock":
        return mock_data.list_by_department(department)
    return await hcm_client.list_by_department(_cfg(c), department)


async def get_management_chain(person_id):
    c = _primary()
    if not c:
        return []
    if c["type"] == "mock":
        return mock_data.get_management_chain(person_id)
    return await hcm_client.get_management_chain(_cfg(c), person_id)


async def overview():
    c = _primary()
    if not c:
        return {"total": 0, "by_department": [], "spans": []}
    if c["type"] == "mock":
        return mock_data.overview()
    return await hcm_client.overview(_cfg(c))


async def run_custom(name, **args):
    """Execute a CONFIG (UI-defined) tool through the primary connector."""
    c = _primary()
    if not c:
        return {"error": "no_source"}
    cfg = tools_registry.get(name)
    if not cfg:
        return {"error": "unknown_tool"}
    if c["type"] == "mock":
        return mock_data.run_custom(cfg, args)
    return await hcm_client.run_custom(_cfg(c), cfg, args)


async def role_signals(person_id):
    """Signals used to infer the signed-in user's role from what Oracle returns:
    report_count, and whether they can see SECURED data beyond their own reports."""
    c = _primary()
    if not c:
        return {"report_count": 0}
    if c["type"] == "mock":
        return mock_data.role_signals(person_id)
    return await hcm_client.role_signals(_cfg(c), person_id)


async def test(connector: dict) -> dict:
    if connector["type"] == "mock":
        return {"ok": True, "status": 200, "count": len(mock_data.WORKERS)}
    return await hcm_client.test_connection(_cfg(connector))
