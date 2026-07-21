"""
Offline DEMO model — a deterministic stand-in for a real LLM (no API key needed).

It drives the REAL agent + MCP tools so the whole app runs end-to-end. It now
handles: person details/contact, "who does X report to" (manager), "who reports
to X" / X's team (direct reports), reporting chain, colleagues (same department),
and department list/count. A real model (Gemini) does true language understanding;
this is a scripted stand-in so the plumbing is observable. Enable with HCM_DEMO=1.
"""
from __future__ import annotations
import re
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel, AgentInfo
import identity as _identity

_FILLER = {
    "get", "me", "the", "details", "detail", "for", "of", "show", "tell", "about", "who",
    "is", "whos", "what", "whats", "find", "look", "up", "information", "info", "on",
    "please", "can", "you", "give", "report", "reports", "reporting", "to", "does", "do",
    "manager", "managers", "boss", "email", "phone", "number", "work", "location", "and",
    "in", "department", "dept", "team", "everyone", "all", "list", "members", "people",
    "a", "an", "his", "her", "their", "contact", "much", "how", "where", "s", "someone",
    "somebody", "called", "named", "person", "employee", "worker", "chain", "above",
    "many", "count", "else", "with", "works", "work", "colleagues", "colleague", "peers",
    "direct", "manage", "manages", "under", "beneath", "hierarchy", "org", "structure",
    "are", "was", "were", "be", "been", "there", "here", "this", "that", "which", "whom", "at",
}


def _latest_user_text(messages) -> str:
    text = ""
    for m in messages:
        for p in getattr(m, "parts", []):
            if p.__class__.__name__ == "UserPromptPart":
                c = p.content
                text = c if isinstance(c, str) else " ".join(str(x) for x in c)
    return text


def _returns(messages) -> dict:
    out = {}
    for m in messages:
        for p in getattr(m, "parts", []):
            if p.__class__.__name__ == "ToolReturnPart":
                out[p.tool_name] = p.content
    return out


def _extract_term(user: str) -> str:
    out = []
    for t in re.split(r"\s+", user.strip()):
        t = t.strip("?!.,:;\"'")
        if t.lower().endswith("'s"):
            t = t[:-2]
        t = re.sub(r"[^\w]", "", t)
        if not t or t.lower() in _FILLER:
            continue
        out.append(t)
    return " ".join(out).strip()


def _classify(user: str) -> str:
    u = " " + user.lower().strip() + " "
    if any(k in u for k in ["who reports to", "direct report", "'s team", "s team ",
                            "who is on", "who's on", " manages", " manage ", "team of",
                            "reports into", "report into"]):
        return "team"
    if any(k in u for k in ["chain", "manager's manager", "managers manager", " above ",
                            "up the", "hierarchy", "who's above", "whos above"]):
        return "chain"
    if any(k in u for k in ["works with", "colleague", "who else is in", "peers",
                            "same team as", "same department"]):
        return "colleagues"
    if any(k in u for k in ["report to", "reports to", " manager", " boss"]) or \
       ("who does" in u and "report" in u):
        return "manager"
    if any(k in u for k in ["everyone", "how many", "list ", "who is in", "who's in",
                            "all the", "members", "people in", "department", "dept"]):
        return "dept"
    return "detail"


def _pick(results, user):
    u = user.lower()
    for r in results:
        if str(r.get("DisplayName", "")).lower() in u:
            return r
    return results[0] if len(results) == 1 else None


def _bullets(rows) -> str:
    return "\n".join(f"• **{r['DisplayName']}** — {r['JobTitle']}"
                     + (f", {r['DepartmentName']}" if r.get("DepartmentName") else "")
                     for r in rows)


def _detail(w: dict) -> str:
    td = ", ".join(x for x in [w.get("JobTitle"), w.get("DepartmentName")] if x)
    lines = [f"**{w.get('DisplayName')}**" + (f" — {td}" if td else "")]
    if w.get("ManagerName"):
        lines.append(f"• **Manager:** {w['ManagerName']}")
    if w.get("LocationName"):
        lines.append(f"• **Location:** {w['LocationName']}")
    if w.get("WorkEmail"):
        lines.append(f"• **Email:** {w['WorkEmail']}")
    if w.get("WorkPhoneNumber"):
        lines.append(f"• **Phone:** {w['WorkPhoneNumber']}")
    tail = []
    if w.get("PersonNumber"):
        tail.append(f"**Employee #:** {w['PersonNumber']}")
    if w.get("AssignmentStatus"):
        tail.append(f"**Status:** {w['AssignmentStatus']}")
    if tail:
        lines.append("• " + "  ·  ".join(tail))
    return "\n".join(lines)


def _demo_fn(messages, info: AgentInfo) -> ModelResponse:
    def call(tool, **args):
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])
    def say(text):
        return ModelResponse(parts=[TextPart(text)])
    try:
        user = _latest_user_text(messages)
        rets = _returns(messages)
        intent = _classify(user)
        term = _extract_term(user) or user

        # ---- self-service ("my", "me", "I") using the signed-in user ----
        cur = _identity.get_current()
        if cur and cur.get("person_number") and re.search(r"\b(my|me|i|mine|myself)\b", user.lower()):
            sid = cur.get("person_id") or cur.get("person_number")
            _low = user.lower()
            if "team" in _low or "report to me" in _low or "reports to me" in _low or "my reports" in _low or "who reports to me" in _low:
                intent = "team"
            elif "chain" in _low or "above me" in _low or "hierarchy" in _low:
                intent = "chain"
            elif "manager" in _low or "boss" in _low or "who do i report" in _low or ("report to" in _low):
                intent = "manager"
            else:
                intent = "detail"
            if intent == "team":
                if cur.get("role") in ("manager", "hr_admin"):
                    if "get_direct_reports" not in rets:
                        return call("get_direct_reports", person_id=sid)
                    reps = rets["get_direct_reports"] or []
                    return say(f"You manage {len(reps)} " + ("person" if len(reps)==1 else "people") + ":\n" + _bullets(reps) if reps else "You have no direct reports on record.")
                return say("You don't have any direct reports — that view is for managers.")
            if intent == "chain":
                if "get_management_chain" not in rets:
                    return call("get_management_chain", person_id=sid)
                c = rets["get_management_chain"] or []
                return say("Your reporting line:\n" + cur["display_name"] + "".join("  \u2192  "+m["DisplayName"]+" ("+m["JobTitle"]+")" for m in c) if c else "You're at the top of the org.")
            if intent == "manager":
                if "get_assignment" not in rets:
                    return call("get_assignment", person_id=sid)
                mgr = rets["get_assignment"].get("ManagerName")
                return say(f"Your manager is **{mgr}**." if mgr else "You have no manager on record.")
            if "get_worker" not in rets:
                return call("get_worker", person_id=sid)
            return say(_detail(rets["get_worker"]))

        # ---- department list / count (no person to resolve) ----
        if intent == "dept":
            if "list_by_department" not in rets:
                return call("list_by_department", department=term)
            members = rets["list_by_department"] or []
            if not members:
                return say(f'I couldn’t find a "{term}" department in Oracle HCM.')
            dept = members[0]["DepartmentName"]
            if any(k in user.lower() for k in ["how many", "count", "number of"]):
                return say(f"There are **{len(members)}** people in **{dept}**:\n" + _bullets(members))
            return say(f"**{dept}** has {len(members)} people:\n" + _bullets(members))

        # ---- everything else: resolve a person first ----
        if "search_workers" not in rets:
            return call("search_workers", query=term)
        results = rets.get("search_workers") or []
        if not results:
            return say(f'I couldn’t find anyone matching "{term}" in Oracle HCM.')
        target = _pick(results, user)
        if target is None:
            opts = "\n".join(f"• {r['DisplayName']} ({r['JobTitle']}, {r['DepartmentName']})" for r in results)
            return say("I found a few people — which one did you mean?\n" + opts)
        pid, name = target["PersonId"], target["DisplayName"]

        if intent == "team":
            if "get_direct_reports" not in rets:
                return call("get_direct_reports", person_id=pid)
            reps = rets["get_direct_reports"] or []
            if not reps:
                return say(f"**{name}** has no direct reports on record.")
            return say(f"**{name}** manages {len(reps)} " + ("person" if len(reps) == 1 else "people") + ":\n" + _bullets(reps))

        if intent == "chain":
            if "get_management_chain" not in rets:
                return call("get_management_chain", person_id=pid)
            chain = rets["get_management_chain"] or []
            if not chain:
                return say(f"**{name}** is at the top of the org — no one above them.")
            path = f"{name}"
            for m in chain:
                path += f"  →  {m['DisplayName']} ({m['JobTitle']})"
            return say(f"Reporting line above **{name}**:\n{path}")

        if intent == "colleagues":
            if "get_assignment" not in rets:
                return call("get_assignment", person_id=pid)
            dept = rets["get_assignment"].get("DepartmentName")
            if "list_by_department" not in rets:
                return call("list_by_department", department=dept)
            peers = [p for p in (rets["list_by_department"] or []) if p["PersonId"] != pid]
            if not peers:
                return say(f"**{name}** is the only person in **{dept}**.")
            return say(f"**{name}**’s colleagues in **{dept}**:\n" + _bullets(peers))

        if intent == "manager":
            if "get_assignment" not in rets:
                return call("get_assignment", person_id=pid)
            mgr = rets["get_assignment"].get("ManagerName")
            return say(f"**{name}** reports to **{mgr}**." if mgr else f"**{name}** is at the top of the org (no manager).")

        # ---- default: detail / contact ----
        if "get_worker" not in rets:
            return call("get_worker", person_id=pid)
        return say(_detail(rets["get_worker"]))
    except Exception as e:
        return ModelResponse(parts=[TextPart(f"(demo model error: {e})")])


def make_demo_model() -> FunctionModel:
    return FunctionModel(_demo_fn, model_name="hcm-demo-model")
