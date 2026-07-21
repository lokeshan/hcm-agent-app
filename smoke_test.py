"""
Keyless smoke test: proves the agent connects to the Oracle HCM MCP server,
discovers its tools, calls them, and receives real (mock) worker data — WITHOUT
needing a Gemini API key. Uses Pydantic AI's FunctionModel to script tool calls.

    python smoke_test.py
"""
from __future__ import annotations
import asyncio

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel, AgentInfo

from agent import hcm_toolset, INSTRUCTIONS

_step = {"n": 0}


def scripted_model(messages, info: AgentInfo) -> ModelResponse:
    """Turn 1: call search_workers. Turn 2: call get_worker. Turn 3: final text."""
    _step["n"] += 1
    if _step["n"] == 1:
        return ModelResponse(parts=[ToolCallPart(tool_name="search_workers", args={"query": "Jane"})])
    if _step["n"] == 2:
        return ModelResponse(parts=[ToolCallPart(tool_name="get_worker", args={"person_id": "300000012345678"})])
    return ModelResponse(parts=[TextPart("Smoke test complete — tools were called and returned data.")])


async def main() -> int:
    # 1) discovered tool names
    agent = Agent(FunctionModel(scripted_model), toolsets=[hcm_toolset()], instructions=INSTRUCTIONS)
    async with agent:
        result = await agent.run("Find Jane Doe and show her details")

    msgs = result.all_messages()
    tool_calls, tool_returns = [], []
    for m in msgs:
        for p in getattr(m, "parts", []):
            cls = p.__class__.__name__
            if cls == "ToolCallPart":
                tool_calls.append(p.tool_name)
            elif cls == "ToolReturnPart":
                tool_returns.append((p.tool_name, str(p.content)[:180]))

    print("TOOL CALLS:", tool_calls)
    for name, content in tool_returns:
        print(f"RETURN[{name}]:", content)
    print("FINAL:", result.output)

    ok = ("search_workers" in tool_calls and "get_worker" in tool_calls
          and any("Jane Doe" in c for _, c in tool_returns))
    print("RESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
