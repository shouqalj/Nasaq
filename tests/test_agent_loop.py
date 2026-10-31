"""Tests the tool-calling loop mechanics and guardrails with a stub client.
(The stub only plays the *model's side of the protocol*; real decisions come from the live API.)"""
from types import SimpleNamespace as NS

from conftest import TODAY, emp
from shiftrescue import agent, scenarios, services as sv


class StubClient:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.seen.append(kw["messages"][-1])
        return self.turns.pop(0)


def tool(name, **inp):
    return NS(type="tool_use", id=f"id_{name}", name=name, input=inp)


def turn(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop)


def text(t):
    return NS(type="text", text=t)


def test_loop_executes_tools_and_logs_timeline(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    client = StubClient([
        turn(text("Analyzing."), tool("analyze_staffing")),
        turn(tool("send_coverage_offer", need_id=1, candidate_id=emp(conn, "Nora Al-Dosari"), rationale="prefers extra")),
        turn(text("Waiting."), stop="end_turn"),
    ])
    assert agent.run_agent(conn, rid, "new_request", client) == 2
    assert conn.execute("SELECT status FROM coverage_offers").fetchone()[0] == "pending"
    events = [r["event_type"] for r in conn.execute("SELECT event_type FROM audit_log WHERE actor='agent'")]
    assert events.count("tool_call") == 2 and "reasoning" in events


def test_agent_cannot_bypass_safeguards(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    client = StubClient([
        turn(tool("analyze_staffing")),
        turn(tool("send_coverage_offer", need_id=1, candidate_id=emp(conn, "Amal Al-Jaber"), rationale="x"),
             tool("submit_to_manager", summary="skip ahead"),
             tool("escalate_to_manager", reason="skip")),
        turn(stop="end_turn"),
    ])
    agent.run_agent(conn, rid, "new_request", client)
    results = client.seen[-1]["content"]
    assert all(r["is_error"] and r["content"].startswith("REFUSED") for r in results)
    assert conn.execute("SELECT COUNT(*) FROM coverage_offers").fetchone()[0] == 0
    assert sv.get_request(conn, rid)["status"] == "seeking_coverage"
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='safeguard_blocked'").fetchone()[0] == 3


def test_agent_cannot_touch_other_requests_needs(conn):
    r1 = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    r2 = scenarios.create_scenario_request(conn, "partial_day", today=TODAY)
    sv.compute_coverage_needs(conn, r1)
    client = StubClient([turn(tool("analyze_staffing")),
                         turn(tool("find_candidates", need_id=1)), turn(stop="end_turn")])
    agent.run_agent(conn, r2, "new_request", client)
    assert client.seen[-1]["content"][0]["is_error"]


def test_safe_run_logs_failure_without_raising(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)

    class Broken:
        messages = NS(create=lambda **k: (_ for _ in ()).throw(RuntimeError("no key")))
    assert agent.safe_run_agent(conn, rid, "new_request", Broken()) is None
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='agent_error'").fetchone()[0] == 1
