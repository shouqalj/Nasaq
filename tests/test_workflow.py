"""Approval sequencing, atomic DB updates, escalation and coverage verification."""
import json

import pytest

from conftest import TODAY, emp
from shiftrescue import scenarios, services as sv


def covered_nurse_request(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    need = sv.compute_coverage_needs(conn, rid)[0]
    o1 = sv.send_offer(conn, need["id"], emp(conn, "Nora Al-Dosari"))
    sv.respond_to_offer(conn, o1, False)                          # first replacement declines
    o2 = sv.send_offer(conn, need["id"], emp(conn, "Khalid Al-Mutairi"))
    sv.respond_to_offer(conn, o2, True)
    return rid, need


def test_decline_reopens_need_and_accept_covers(conn):
    rid, need = covered_nurse_request(conn)
    statuses = [r["status"] for r in conn.execute("SELECT status FROM coverage_offers ORDER BY id")]
    assert statuses == ["declined", "accepted"]
    assert conn.execute("SELECT status FROM coverage_needs").fetchone()[0] == "covered"


def test_schedule_not_changed_before_approval(conn):
    rid, need = covered_nurse_request(conn)
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE source='coverage'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM shifts WHERE id=?", (need["shift_id"],)).fetchone()[0] == "scheduled"


def test_sequencing_manager_cannot_approve_before_coverage(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    with pytest.raises(sv.ServiceError, match="not allowed yet"):   # still 'submitted'
        sv.manager_decide(conn, rid, True)
    sv.compute_coverage_needs(conn, rid)
    with pytest.raises(sv.ServiceError, match="not allowed yet"):   # seeking coverage
        sv.manager_decide(conn, rid, True)
    with pytest.raises(sv.ServiceError, match="coverage not secured"):
        sv.submit_to_manager(conn, rid)


def test_cannot_escalate_while_candidates_remain(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    sv.compute_coverage_needs(conn, rid)
    with pytest.raises(sv.ServiceError, match="eligible candidates"):
        sv.escalate(conn, rid, "gave up early")


def test_full_approval_updates_everything_atomically(conn):
    rid, need = covered_nurse_request(conn)
    reem, khalid = emp(conn, "Reem Al-Ghamdi"), emp(conn, "Khalid Al-Mutairi")
    before = conn.execute("SELECT leave_balance_hours FROM employees WHERE id=?", (reem,)).fetchone()[0]
    sv.submit_to_manager(conn, rid, "ok")
    assert sv.get_request(conn, rid)["status"] == "awaiting_manager"
    result = sv.manager_decide(conn, rid, True, "approved")
    assert result["covered"] is True
    req = sv.get_request(conn, rid)
    assert req["status"] == "approved" and req["verified"] == 1
    assert conn.execute("SELECT leave_balance_hours FROM employees WHERE id=?", (reem,)).fetchone()[0] == before - 8
    assert conn.execute("SELECT status FROM shifts WHERE id=?", (need["shift_id"],)).fetchone()[0] == "on_leave"
    rep = conn.execute("SELECT * FROM shifts WHERE source='coverage'").fetchone()
    assert (rep["employee_id"], rep["start_hour"], rep["end_hour"]) == (khalid, 7, 15)
    assert conn.execute("SELECT COUNT(*) FROM schedule_changes WHERE request_id=?", (rid,)).fetchone()[0] >= 3
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM notifications")}
    assert {"approved", "shift_confirmed"} <= kinds
    events = [r["event_type"] for r in conn.execute("SELECT event_type FROM audit_log WHERE request_id=?", (rid,))]
    assert "manager_approved" in events and "schedule_updated" in events


def test_apply_rolls_back_on_failure(conn, monkeypatch):
    rid, need = covered_nurse_request(conn)
    reem = emp(conn, "Reem Al-Ghamdi")
    sv.submit_to_manager(conn, rid)

    def boom(*a, **k):
        raise RuntimeError("disk exploded")
    monkeypatch.setattr(sv, "_write_replacement_shift", boom)
    with pytest.raises(RuntimeError):
        sv.manager_decide(conn, rid, True)
    assert sv.get_request(conn, rid)["status"] == "awaiting_manager"
    assert conn.execute("SELECT leave_balance_hours FROM employees WHERE id=?", (reem,)).fetchone()[0] == 120
    assert conn.execute("SELECT status FROM shifts WHERE id=?", (need["shift_id"],)).fetchone()[0] == "scheduled"
    assert conn.execute("SELECT COUNT(*) FROM schedule_changes").fetchone()[0] == 0


def test_partial_day_splits_shift_and_verifies(conn):
    rid = scenarios.create_scenario_request(conn, "partial_day", today=TODAY)
    need = sv.compute_coverage_needs(conn, rid)[0]
    assert (need["start_hour"], need["end_hour"]) == (12, 16)
    o = sv.send_offer(conn, need["id"], emp(conn, "Tariq Al-Subaie"))
    sv.respond_to_offer(conn, o, True)
    sv.submit_to_manager(conn, rid)
    res = sv.manager_decide(conn, rid, True)
    assert res == {"covered": True, "gaps": []}
    maha = emp(conn, "Maha Al-Anazi")
    rows = [(r["start_hour"], r["end_hour"], r["status"]) for r in conn.execute(
        "SELECT * FROM shifts WHERE employee_id=? AND date=? ORDER BY start_hour", (maha, need["date"]))]
    assert rows == [(8, 12, "scheduled"), (12, 16, "on_leave")]
    assert conn.execute("SELECT leave_balance_hours FROM employees WHERE id=?", (maha,)).fetchone()[0] == 116


def test_no_replacement_escalates_and_never_denies(conn):
    rid = scenarios.create_scenario_request(conn, "no_replacement", today=TODAY)
    need = sv.compute_coverage_needs(conn, rid)[0]
    assert sv.find_candidates(conn, need["id"])["eligible_candidates"] == []
    sv.escalate(conn, rid, "No other pharmacist available")
    req = sv.get_request(conn, rid)
    assert req["status"] == "escalated" and req["status"] != "rejected"
    # manager must explicitly accept the gap
    with pytest.raises(sv.ServiceError, match="coverage gap"):
        sv.manager_decide(conn, rid, True)
    res = sv.manager_decide(conn, rid, True, accept_uncovered_gap=True)
    assert res["covered"] is False and res["gaps"]            # verification honestly reports the gap
    assert sv.get_request(conn, rid)["verified"] == 0


def test_manager_reject_leaves_schedule_untouched(conn):
    rid, need = covered_nurse_request(conn)
    sv.submit_to_manager(conn, rid)
    sv.manager_decide(conn, rid, False, "busy week")
    assert sv.get_request(conn, rid)["status"] == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE source='coverage'").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM shifts WHERE id=?", (need["shift_id"],)).fetchone()[0] == "scheduled"


def test_stale_replacement_blocks_approval_and_reopens_search(conn):
    rid, need = covered_nurse_request(conn)
    sv.submit_to_manager(conn, rid)
    khalid = emp(conn, "Khalid Al-Mutairi")
    conn.execute("INSERT INTO shifts(employee_id,date,start_hour,end_hour,role) VALUES (?,?,?,?,?)",
                 (khalid, need["date"], 9, 12, "Nurse"))        # schedule changed meanwhile
    with pytest.raises(sv.ServiceError, match="no longer eligible"):
        sv.manager_decide(conn, rid, True)
    assert sv.get_request(conn, rid)["status"] == "seeking_coverage"
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE source='coverage'").fetchone()[0] == 0


def test_verify_requires_approval(conn):
    rid, _ = covered_nurse_request(conn)
    with pytest.raises(sv.ServiceError):
        sv.verify_coverage(conn, rid)
