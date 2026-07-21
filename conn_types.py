"""Connector-type catalog (§8.3 / A2) — the *types* of data source an admin can add.

Engineers add new TYPES here (code); admins then add SOURCES of a type from
/admin/sources (config). Each type declares the config fields its form needs.
"""
from __future__ import annotations

TYPES = {
    "mock": {
        "label": "Mock (sample data)",
        "status": "available",
        "blurb": "Built-in 9-person demo directory. No configuration.",
        "fields": [],
    },
    "oracle_hcm": {
        "label": "Oracle HCM Cloud",
        "status": "available",
        "blurb": "Oracle HCM REST (workers resource). Basic or OAuth2. Per-user sign-in supported.",
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "text",
             "placeholder": "https://<pod>.fa.ocs.oraclecloud.com"},
            {"key": "auth", "label": "Auth", "type": "select", "options": ["basic", "oauth"]},
            {"key": "username", "label": "Username (Basic)", "type": "text"},
            {"key": "password", "label": "Password (Basic)", "type": "password"},
            {"key": "client_id", "label": "Client ID (OAuth2)", "type": "text"},
            {"key": "client_secret", "label": "Client secret (OAuth2)", "type": "password"},
            {"key": "token_url", "label": "Token URL (OAuth2)", "type": "text"},
            {"key": "scope", "label": "Scope (OAuth2, optional)", "type": "text"},
        ],
    },
    "servicenow": {
        "label": "ServiceNow (HR cases)",
        "status": "planned",
        "blurb": "Raise/track HR cases. Surfaces namespaced tools (e.g. snow_raise_case).",
        "fields": [
            {"key": "base_url", "label": "Instance URL", "type": "text"},
            {"key": "username", "label": "Username", "type": "text"},
            {"key": "password", "label": "Password", "type": "password"},
        ],
    },
    "knowledge_base": {
        "label": "Knowledge Base (policies)",
        "status": "planned",
        "blurb": "Policy / handbook Q&A source for the Policies page.",
        "fields": [
            {"key": "base_url", "label": "KB URL", "type": "text"},
            {"key": "api_key", "label": "API key", "type": "password"},
        ],
    },
    "external_mcp": {
        "label": "External MCP server",
        "status": "available",
        "blurb": "Mount a third-party MCP server; its tools surface namespaced per source. "
                 "Each remote tool still needs an enabled tool-registry row under its prefixed name.",
        "fields": [
            {"key": "mcp_url", "label": "MCP server URL / script path", "type": "text",
             "placeholder": "https://example.com/mcp"},
            {"key": "mcp_auth", "label": "Auth", "type": "select",
             "options": ["none", "bearer", "oauth"]},
            {"key": "mcp_token", "label": "Bearer token / API key", "type": "password"},
            {"key": "mcp_prefix", "label": "Tool name prefix", "type": "text",
             "placeholder": "workday"},
        ],
    },
}


def list_types() -> list:
    return [{"type": t, **meta} for t, meta in TYPES.items()]


def get_type(t: str) -> dict | None:
    m = TYPES.get(t)
    return {"type": t, **m} if m else None
