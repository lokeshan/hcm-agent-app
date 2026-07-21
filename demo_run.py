"""
Run the application end-to-end (no API key) and print the full flow for several
questions: the question, the real MCP tool calls made, and the composed answer.
Also writes demo_transcript.json for the chat visual.

    HCM_DEMO=1 python demo_run.py     (env set automatically below)
"""
from __future__ import annotations
import asyncio, os, json
os.environ.setdefault("HCM_DEMO", "1")
from agent import build_agent

QUERIES = [
    "Get me the details for Jane Doe",
    "Who does Jane Doe report to?",
    "Who reports to Priya Nair?",
    "Show me the reporting chain above Jane Doe",
    "Who else is in Ahmed Khan's department?",
    "How many people are in Engineering?",
    "Show me everyone in Finance",
    "Find someone called Bob Marley",
]


def _args(p):
    a = getattr(p, "args", None)
    if isinstance(a, str):
        try: return json.loads(a)
        except Exception: return {"_raw": a}
    return a or {}


async def run_one(q: str) -> dict:
    agent = build_agent()  # HCM_DEMO -> deterministic demo model + real MCP tools
    async with agent:
        result = await agent.run(q)
    calls = []
    for m in result.all_messages():
        for p in getattr(m, "parts", []):
            if p.__class__.__name__ == "ToolCallPart":
                calls.append({"tool": p.tool_name, "args": _args(p)})
    return {"q": q, "answer": result.output, "tool_calls": calls}


async def main():
    transcript = []
    for q in QUERIES:
        item = await run_one(q)
        transcript.append(item)
        print("=" * 72)
        print("USER:      ", q)
        for c in item["tool_calls"]:
            print(f"   -> MCP tool: {c['tool']}({', '.join(f'{k}={v!r}' for k, v in c['args'].items())})")
        print("ASSISTANT:")
        for line in item["answer"].splitlines():
            print("   " + line)
    json.dump(transcript, open("demo_transcript.json", "w"), indent=2)
    print("=" * 72)
    print("Saved demo_transcript.json (", len(transcript), "turns )")


if __name__ == "__main__":
    asyncio.run(main())
