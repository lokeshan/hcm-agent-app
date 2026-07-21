"""The HR Assistant agent (Pydantic AI) — model from config, tools via in-memory MCP."""
from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.mcp import MCPToolset

import config
import mcp_sources
import tools_registry as registry
from hcm_mcp_server import mcp as hcm_mcp, sync_custom_tools

INSTRUCTIONS = """\
You are "HR Assistant", answering employee questions via the provided Oracle HCM tools.
- Use ONLY the tools for facts about people; never invent data.
- Resolve a person with search_workers first, then get_worker / get_assignment /
  get_direct_reports / list_by_department / get_management_chain as needed.
- Goals & learning: get_goals (performance goals), get_development_goals, and
  get_learning take a PERSON NUMBER, not a person id — take it from the current
  user, or from PersonNumber on a search_workers result. get_team_goals takes a
  manager's person id and returns every direct report's goals.
- If a goals or learning lookup comes back empty, say so plainly — it means that
  person has no records, not that you should retry with a different tool.
- If several people match, list them and ask which one. If none match, say so.
- Present results cleanly (name, title, department, manager, contact). Be concise.
"""


def hcm_toolset(role: str | None = None) -> MCPToolset:
    # In-memory MCP: tools run in this process, so the cache persists across messages.
    ts = MCPToolset(hcm_mcp)
    if role:
        allowed = registry.allowed_names_for_role(role)
        try:  # hand the model only the tools this role may use (governance);
            ts = ts.filtered(lambda ctx, td: td.name in allowed)  # tool layer also enforces.
        except Exception:
            pass
    return ts


def _model_from_config():
    cfg = config.load()
    prov = cfg.get("ai_provider", "demo")
    key = cfg.get("ai_api_key", "")
    name = cfg.get("ai_model") or config.default_model(prov)
    if prov == "demo" or not key:
        from demo_model import make_demo_model
        return make_demo_model()
    if prov == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider
        return GoogleModel(name, provider=GoogleProvider(api_key=key))
    if prov == "openai":
        from pydantic_ai.models.openai import OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider
        return OpenAIModel(name, provider=OpenAIProvider(api_key=key))
    if prov == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        return AnthropicModel(name, provider=AnthropicProvider(api_key=key))
    from demo_model import make_demo_model
    return make_demo_model()


_ROLE_NOTE = {
    "employee": "This user is an individual contributor — focus on their own profile, their manager, and the public directory.",
    "manager": "This user is a people manager — they can ask about their team / direct reports and each report's details.",
    "hr_admin": "This user has HR-level access — they may look up the full worker records Oracle returns to them.",
}


def _role_context(u: dict) -> str:
    role = u.get("role", "employee")
    return (
        f"CURRENT USER: {u.get('display_name')} (employee #{u.get('person_number')}, role: {role}).\n"
        "Oracle HCM enforces data security for the signed-in user, so the tools only ever return\n"
        "data this user is permitted to see. Present whatever the tools return; do NOT refuse based\n"
        "on your own guess about permissions, and never invent data. 'me / my / I' means this user.\n"
        f"- {_ROLE_NOTE.get(role, _ROLE_NOTE['employee'])}\n"
    )


def build_agent(model=None, identity: dict | None = None) -> Agent:
    sync_custom_tools()   # register any UI-added tools before the agent enumerates them
    if model is None:
        model = _model_from_config()
    role = (identity or {}).get("role", "employee")
    instr = INSTRUCTIONS
    if identity and identity.get("display_name"):
        instr = instr + "\n\n" + _role_context(identity)
    # Filter the toolset by role for real LLMs (they choose tools dynamically).
    # The scripted demo brain calls tools by name, so we DON'T filter there —
    # the in-tool governance wrapper still enforces + audits every call.
    filter_role = None if is_demo() else role
    ts = hcm_toolset(filter_role)
    # A write tool can be invoked by the model on its own reading of a sentence, so
    # require the human to confirm it. Governance decides who MAY call a tool; this
    # decides that this particular call happens now. Reads are untouched.
    writes = registry.write_tool_names()
    if writes:
        ts = ts.approval_required(lambda ctx, td, args: td.name in writes)
    toolsets = [ts]
    # External MCP servers: always role-filtered (the demo brain doesn't know them),
    # and never allowed to break chat — a broken server just contributes no tools.
    try:
        toolsets += mcp_sources.toolsets_for_agent(role)
    except Exception as e:
        print("[mcp] external toolsets unavailable:", type(e).__name__, e)
    # DeferredToolRequests lets a run end by ASKING rather than answering; the chat
    # endpoint turns that into an approve/cancel prompt and resumes from history.
    return Agent(model, toolsets=toolsets, instructions=instr,
                 output_type=[str, DeferredToolRequests])


def is_demo() -> bool:
    cfg = config.load()
    return cfg.get("ai_provider", "demo") == "demo" or not cfg.get("ai_api_key")


def active_mode() -> str:
    cfg = config.load()
    prov = cfg.get("ai_provider", "demo")
    key = cfg.get("ai_api_key", "")
    model = "offline demo brain" if (prov == "demo" or not key) else f"{prov}:{cfg.get('ai_model') or config.default_model(prov)}"
    import connectors
    p = connectors.primary()
    src = p["name"] if p else "no source"
    return f"AI: {model} · Data: {src}"
