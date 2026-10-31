"""The LLM agent. Claude decides which tools to call and when; every tool is a thin
wrapper over services.py, which enforces all hard constraints deterministically."""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv

from . import services as sv

load_dotenv()

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
MAX_TURNS = 24

SYSTEM = """You are Nasaq, an autonomous operations agent that owns the leave & shift-coverage process for a clinic.
You work through tools only. Deterministic safeguards (qualifications, availability, rest, weekly hours, minimum
staffing, balances, approval order) are enforced by the tools - if a tool refuses, read the reason and adapt; never retry the same
refused action.

You may NEVER approve or deny leave: only the manager decides. You cannot change the schedule directly; it is
updated automatically after manager approval.

Workflow for a new request: get_request_context -> get_policies -> analyze_staffing -> for each open coverage need call
find_candidates, pick the best candidate using your judgement (the list is a hint ordering; weigh preference for extra shifts,
hours already worked, seniority, fairness) and send_coverage_offer with a short rationale. Send ONE offer at a time per need.
After sending an offer, stop and say you are waiting for the response (do not poll).
When an offer was declined: call get_request_context, then find_candidates again and offer to the next best candidate.
When every need is covered: submit_to_manager. If no eligible candidate remains (or the offer limit is reached),
escalate_to_manager with a clear explanation - never deny. If staffing remains satisfied without a replacement, go straight
to submit_to_manager.
After manager approval was applied: call verify_coverage, then send_notification to the employee (and the manager) with a brief
confirmation of the verified result.
Before each action write one short sentence explaining what you are doing and why.

Multi-day leave: a request can span up to 7 days and produce several coverage needs (one per affected shift segment, each with its own
date and hours). Handle each need independently - different needs may get different replacements, offers for different needs may be
outstanding at the same time, and each offer is accepted or declined on its own. Only call submit_to_manager once EVERY need is covered.
If some needs cannot be covered after the candidate pool is exhausted, wait for any pending offers to resolve, then escalate_to_manager
naming the specific uncovered dates/shifts; the covered ones stay covered.

Sick leave (leave_type = sick): the absence is ALREADY recorded as Reported and the employee's shifts are already marked absent. It needs
no manager approval and you must not call submit_to_manager for it. Your job is to assess the staffing impact (analyze_staffing), find and offer
replacements per need exactly as above (an accepted replacement is written to the schedule immediately), and if nobody can cover a need,
escalate_to_manager - which flags the staffing shortage but never blocks or denies the sick absence. When coverage work is finished, call
verify_coverage and send_notification to the manager with the coverage status. Never describe sick leave as approved or eligible; that is an HR
decision outside this system."""

EVENT_HINTS = {
    "new_request": "A new leave request was just submitted. Handle it from the start.",
    "offer_declined": "A coverage offer was just DECLINED. Continue the search for the affected need.",
    "offer_accepted": "A coverage offer was just ACCEPTED. Check whether all needs are covered and route onward (annual: manager; sick: verify and notify).",
    "sick_reported": "A sick absence was just reported and already recorded. Assess staffing impact and arrange coverage where needed.",
    "approved_and_applied": "The manager approved and the schedule was updated. Verify coverage and notify the people involved.",
    "manual_rerun": "Manual re-run requested. Inspect the current state and take the next appropriate step (or do nothing if waiting).",
}


def _tool(name, desc, props, required):
    return {"name": name, "description": desc, "strict": True,
            "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}


_I = {"type": "integer"}
_S = {"type": "string"}
TOOLS = [
    _tool("get_request_context", "Get the leave request, the employee (role, leave balance), their shifts that day, and current coverage needs/offers.", {}, []),
    _tool("get_policies", "Get company scheduling and leave policies.", {}, []),
    _tool("analyze_staffing", "Identify affected shifts and check minimum staffing; creates coverage needs for any shortfall. Safe to call repeatedly.", {}, []),
    _tool("find_candidates", "List eligible replacements for a coverage need (rules already applied) plus why others were excluded.",
          {"need_id": _I}, ["need_id"]),
    _tool("send_coverage_offer", "Send an in-app coverage offer to one eligible candidate. Refused if candidate is ineligible or an offer is pending.",
          {"need_id": _I, "candidate_id": _I, "rationale": _S}, ["need_id", "candidate_id", "rationale"]),
    _tool("submit_to_manager", "Route the request to the manager for approval. Refused unless all coverage needs are covered.",
          {"summary": _S}, ["summary"]),
    _tool("escalate_to_manager", "Escalate when no replacement can be found. Does NOT deny leave. Refused if eligible candidates remain.",
          {"reason": _S}, ["reason"]),
    _tool("verify_coverage", "Verify minimum staffing is met for the leave window. Use after manager approval (annual) or when coverage work on a reported sick absence is finished.", {}, []),
    _tool("send_notification", "Send an in-app message to the requesting employee or the manager about this request.",
          {"recipient": {"type": "string", "enum": ["employee", "manager"]}, "message": _S}, ["recipient", "message"]),
]


def _dispatch(conn, rid, name, a):
    if name == "get_request_context":
        return sv.request_context(conn, rid)
    if name == "get_policies":
        return sv.get_policies(conn)
    if name == "analyze_staffing":
        return {"coverage_needs": sv.compute_coverage_needs(conn, rid)}
    if name == "find_candidates":
        return sv.find_candidates(conn, a["need_id"], rid)
    if name == "send_coverage_offer":
        sv.get_need(conn, a["need_id"], rid)
        oid = sv.send_offer(conn, a["need_id"], a["candidate_id"], a["rationale"], request_id=rid)
        return {"offer_id": oid, "status": "pending", "note": "Waiting for the employee's response."}
    if name == "submit_to_manager":
        sv.submit_to_manager(conn, rid, a["summary"])
        return {"status": "awaiting_manager"}
    if name == "escalate_to_manager":
        sv.escalate(conn, rid, a["reason"])
        return {"status": "escalated"}
    if name == "verify_coverage":
        res = sv.verify_coverage(conn, rid)
        sv.audit(conn, rid, "agent", "coverage_verified", "Coverage verified: " + ("OK" if res["covered"] else f"GAPS {res['gaps']}"), res)
        return res
    if name == "send_notification":
        req = sv.get_request(conn, rid)
        to = req["employee_id"] if a["recipient"] == "employee" else req["manager_id"]
        if not to:
            raise sv.ServiceError("recipient not found")
        sv.notify(conn, to, rid, "agent_update", a["message"])
        return {"sent": True}
    raise sv.ServiceError(f"unknown tool {name}")


def make_client():
    import anthropic
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


def run_agent(conn, request_id, event="manual_rerun", client=None, max_turns=MAX_TURNS):
    """Run one agent episode. Returns number of tool calls made."""
    client = client or make_client()
    sv.audit(conn, request_id, "system", "agent_invoked", f"Agent invoked (event: {event})")
    messages = [{"role": "user", "content": f"Event: {event}\nLeave request id: {request_id}\n{EVENT_HINTS.get(event, '')}"}]
    calls = 0
    for _ in range(max_turns):
        resp = client.messages.create(model=MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages)
        if resp.stop_reason == "refusal":
            sv.audit(conn, request_id, "agent", "agent_error", "Model refused to continue; a human should take over.")
            break
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                sv.audit(conn, request_id, "agent", "reasoning", b.text.strip())
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            break
        results = []
        for b in uses:
            calls += 1
            args = dict(b.input or {})
            sv.audit(conn, request_id, "agent", "tool_call", f"{b.name}({json.dumps(args)})", {"tool": b.name, "args": args})
            try:
                out = _dispatch(conn, request_id, b.name, args)
                sv.audit(conn, request_id, "agent", "tool_result", f"{b.name} OK", {"tool": b.name})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)})
            except sv.ServiceError as e:
                sv.audit(conn, request_id, "system", "safeguard_blocked", f"{b.name} refused: {e}", {"tool": b.name})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"REFUSED: {e}", "is_error": True})
            except Exception as e:  # bad arguments etc.
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"ERROR: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})
    else:
        sv.audit(conn, request_id, "system", "agent_error", f"Agent hit the {max_turns}-turn limit")
    return calls


def safe_run_agent(conn, request_id, event, client=None):
    """Never raises - failures (e.g. missing API key) are logged to the timeline."""
    try:
        return run_agent(conn, request_id, event, client)
    except Exception as e:
        sv.audit(conn, request_id, "system", "agent_error", f"Agent unavailable: {type(e).__name__}: {e}")
        return None
