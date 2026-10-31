"""Multi-day leave and sick-absence workflow tests (no API calls)."""
import json
from datetime import timedelta
import sqlite3

import pytest

from conftest import BASE, TODAY, day, emp
from shiftrescue import agent, db, rules, scenarios, services as sv
from test_agent_loop import StubClient, text, tool, turn


# ---------------------------------------------------------------- helpers
def layla_week(conn):
    rid = scenarios.create_scenario_request(conn, "multi_day", today=TODAY)
    needs = sv.compute_coverage_needs(conn, rid)
    return rid, needs


def offer(conn, need, name, accept):
    oid = sv.send_offer(conn, need["id"], emp(conn, name), "test")
    sv.respond_to_offer(conn, oid, accept)
    return oid


def cover_all(conn, needs):
    offer(conn, needs[0], "Nora Al-Dosari", False)       # first Monday candidate declines
    offer(conn, needs[0], "Khalid Al-Mutairi", True)
    offer(conn, needs[1], "Nora Al-Dosari", True)
    offer(conn, needs[2], "Yousef Al-Otaibi", True)


# ---------------------------------------------------------------- multi-day annual
def test_hours_count_only_scheduled_days(conn):
    rid, needs = layla_week(conn)
    req = sv.get_request(conn, rid)
    assert (req["date"], req["end_date"]) == (day(0), day(4))
    assert req["leave_hours"] == 24          # 5 calendar days, only 3 scheduled shifts of 8h
    assert [(n["date"], n["start_hour"], n["end_hour"]) for n in needs] == [
        (day(0), 7, 15), (day(2), 15, 23), (day(4), 7, 15)]


def test_candidate_pools_differ_per_day_and_rules_still_apply(conn):
    rid, needs = layla_week(conn)
    names = lambda n: {c["name"] for c in sv.find_candidates(conn, n["id"])["eligible_candidates"]}
    assert names(needs[1]) == {"Nora Al-Dosari", "Khalid Al-Mutairi"}      # evening: rest before Thu 07:00 excludes others
    assert names(needs[2]) == {"Yousef Al-Otaibi"}                        # Friday: rest + weekly cap shrink the pool
    assert "Huda Al-Zahrani" not in names(needs[0])                       # part-time cap


def test_independent_offers_and_out_of_order_responses(conn):
    rid, needs = layla_week(conn)
    o0 = sv.send_offer(conn, needs[0]["id"], emp(conn, "Nora Al-Dosari"), "a")
    o1 = sv.send_offer(conn, needs[1]["id"], emp(conn, "Khalid Al-Mutairi"), "b")
    o2 = sv.send_offer(conn, needs[2]["id"], emp(conn, "Yousef Al-Otaibi"), "c")
    sv.respond_to_offer(conn, o2, True)
    sv.respond_to_offer(conn, o0, False)
    sv.respond_to_offer(conn, o1, True)
    st = {n["date"]: n["status"] for n in sv.describe_needs(conn, rid)}
    assert st == {day(0): "open", day(2): "covered", day(4): "covered"}
    with pytest.raises(sv.ServiceError, match="coverage not secured"):    # one need still open
        sv.submit_to_manager(conn, rid)


def test_reserved_offer_blocks_adjacent_rest_violation(conn):
    rid, needs = layla_week(conn)
    yousef = emp(conn, "Yousef Al-Otaibi")
    probe = {"id": 9999, "request_id": rid, "role": "Nurse", "date": day(5), "start_hour": 0, "end_hour": 4}
    assert rules.check_candidate(conn, probe, yousef) == []
    sv.send_offer(conn, needs[2]["id"], yousef, "fri")                     # holds Fri 07-15
    assert any("rest rule" in r for r in rules.check_candidate(conn, probe, yousef))


def test_full_multiday_approval_is_atomic_and_verified(conn):
    rid, needs = layla_week(conn)
    cover_all(conn, needs)
    layla = emp(conn, "Layla Al-Harbi")
    before_shifts = conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
    sv.submit_to_manager(conn, rid, "all covered")
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE source='coverage'").fetchone()[0] == 0   # nothing before approval
    res = sv.manager_decide(conn, rid, True)
    assert res == {"covered": True, "gaps": []}
    assert conn.execute("SELECT leave_balance_hours FROM employees WHERE id=?", (layla,)).fetchone()[0] == 96
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE leave_request_id=? AND status='on_leave'", (rid,)).fetchone()[0] == 3
    reps = conn.execute("SELECT date,employee_id FROM shifts WHERE source='coverage' ORDER BY date").fetchall()
    assert [r["date"] for r in reps] == [day(0), day(2), day(4)]
    assert len({r["employee_id"] for r in reps}) == 3                      # three different replacements
    assert conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == before_shifts + 3


def test_multiday_apply_rolls_back_everything_on_failure(conn, monkeypatch):
    rid, needs = layla_week(conn)
    cover_all(conn, needs)
    sv.submit_to_manager(conn, rid)
    calls = {"n": 0}
    real = sv._write_replacement_shift

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:                  # fail on the LAST day after two replacements were written
            raise RuntimeError("boom")
        return real(*a, **k)
    monkeypatch.setattr(sv, "_write_replacement_shift", flaky)
    with pytest.raises(RuntimeError):
        sv.manager_decide(conn, rid, True)
    assert sv.get_request(conn, rid)["status"] == "awaiting_manager"
    assert conn.execute("SELECT leave_balance_hours FROM employees WHERE id=?", (emp(conn, "Layla Al-Harbi"),)).fetchone()[0] == 120
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE source='coverage' OR status='on_leave'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM schedule_changes").fetchone()[0] == 0


def test_partial_coverage_escalates_only_uncovered_period(conn):
    rid, needs = layla_week(conn)
    offer(conn, needs[0], "Khalid Al-Mutairi", True)
    offer(conn, needs[1], "Nora Al-Dosari", True)
    offer(conn, needs[2], "Yousef Al-Otaibi", False)                       # only Friday candidate declines
    assert sv.find_candidates(conn, needs[2]["id"])["eligible_candidates"] == []
    sv.escalate(conn, rid, "No one can cover Friday")
    st = {n["date"]: n["status"] for n in sv.describe_needs(conn, rid)}
    assert st == {day(0): "covered", day(2): "covered", day(4): "uncovered"}
    assert sv.get_request(conn, rid)["status"] == "escalated"
    with pytest.raises(sv.ServiceError, match="coverage gap"):
        sv.manager_decide(conn, rid, True)
    res = sv.manager_decide(conn, rid, True, accept_uncovered_gap=True)
    assert res["covered"] is False and {g["date"] for g in res["gaps"]} == {day(4)}
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE source='coverage'").fetchone()[0] == 2


def test_multiday_input_validation(conn):
    layla = emp(conn, "Layla Al-Harbi")
    with pytest.raises(sv.ServiceError, match="at most 7"):
        sv.create_leave_request(conn, layla, day(0), end_date=day(7), today=TODAY)
    with pytest.raises(sv.ServiceError, match="before start"):
        sv.create_leave_request(conn, layla, day(3), end_date=day(1), today=TODAY)
    with pytest.raises(sv.ServiceError, match="full days"):
        sv.create_leave_request(conn, layla, day(0), 8, 12, end_date=day(1), today=TODAY)
    sv.create_leave_request(conn, layla, day(0), end_date=day(4), today=TODAY)
    with pytest.raises(sv.ServiceError, match="overlaps"):
        sv.create_leave_request(conn, layla, day(2), today=TODAY)


def test_multiday_balance_checked_on_total_hours(conn):
    conn.execute("UPDATE employees SET leave_balance_hours=16 WHERE name='Layla Al-Harbi'")
    with pytest.raises(sv.ServiceError, match="insufficient leave balance"):
        sv.create_leave_request(conn, emp(conn, "Layla Al-Harbi"), day(0), end_date=day(4), today=TODAY)


def test_single_day_and_hourly_still_work(conn):
    rid = scenarios.create_scenario_request(conn, "partial_day", today=TODAY)
    req = sv.get_request(conn, rid)
    assert req["end_date"] == req["date"] and (req["start_hour"], req["end_hour"]) == (12, 16)


# ---------------------------------------------------------------- sick leave
def sick_hessa(conn):
    rid = scenarios.create_scenario_request(conn, "sick_multi_day", today=TODAY)
    return rid


def test_sick_is_reported_immediately_without_approval_or_balance_change(conn):
    hessa = emp(conn, "Hessa Al-Rashid")
    rid = sick_hessa(conn)
    req = sv.get_request(conn, rid)
    assert req["status"] == "reported" and req["leave_type"] == "sick"
    assert conn.execute("SELECT leave_balance_hours FROM employees WHERE id=?", (hessa,)).fetchone()[0] == 120
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE leave_request_id=? AND status='on_leave'", (rid,)).fetchone()[0] == 2
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM notifications")]
    assert "sick_reported" in kinds
    with pytest.raises(sv.ServiceError, match="not approved"):
        sv.manager_decide(conn, rid, True)
    with pytest.raises(sv.ServiceError, match="no manager approval"):
        sv.submit_to_manager(conn, rid)


def test_sick_allows_today_but_annual_does_not(conn):
    layla = emp(conn, "Layla Al-Harbi")                                    # works Monday 07-15
    with pytest.raises(sv.ServiceError, match="future"):
        sv.create_leave_request(conn, layla, day(0), today=BASE)
    assert sv.create_leave_request(conn, layla, day(0), leave_type="sick", today=BASE)
    with pytest.raises(sv.ServiceError, match="today or a future"):
        sv.create_leave_request(conn, emp(conn, "Omar Al-Qahtani"), day(0), leave_type="sick", today=BASE + timedelta(days=1))


def test_sick_coverage_applied_immediately_and_independently_per_day(conn):
    rid = sick_hessa(conn)
    needs = sv.compute_coverage_needs(conn, rid)
    assert [n["date"] for n in needs] == [day(3), day(4)]
    assert sv.get_request(conn, rid)["status"] == "reported"
    o1 = sv.send_offer(conn, needs[0]["id"], emp(conn, "Tariq Al-Subaie"), "thu")
    sv.respond_to_offer(conn, o1, True)
    shifts = conn.execute("SELECT * FROM shifts WHERE source='coverage'").fetchall()
    assert len(shifts) == 1 and shifts[0]["date"] == day(3)                # written without any manager step
    assert sv.get_request(conn, rid)["status"] == "reported"
    o2 = sv.send_offer(conn, needs[1]["id"], emp(conn, "Maha Al-Anazi"), "fri")
    sv.respond_to_offer(conn, o2, False)                                   # declines -> need reopens
    assert sv.describe_needs(conn, rid)[1]["status"] == "open"
    o3 = sv.send_offer(conn, needs[1]["id"], emp(conn, "Bader Al-Harthi"), "fri")
    sv.respond_to_offer(conn, o3, True)
    assert sv.verify_coverage(conn, rid) == {"covered": True, "gaps": []}
    assert sv.coverage_summary(conn, rid)["state"] == "covered"
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='sick_coverage'").fetchone()[0] == 2


def test_sick_replacements_still_obey_all_safeguards(conn):
    rid = sick_hessa(conn)
    needs = sv.compute_coverage_needs(conn, rid)
    with pytest.raises(sv.ServiceError, match="not eligible"):
        sv.send_offer(conn, needs[0]["id"], emp(conn, "Layla Al-Harbi"), "wrong role")
    with pytest.raises(sv.ServiceError, match="not eligible"):
        sv.send_offer(conn, needs[0]["id"], emp(conn, "Hessa Al-Rashid"), "the sick employee")


def test_sick_with_no_replacement_escalates_without_blocking(conn):
    rid = sv.create_leave_request(conn, emp(conn, "Rana Al-Tamimi"), day(0), leave_type="sick", today=TODAY)
    need = sv.compute_coverage_needs(conn, rid)[0]
    assert sv.find_candidates(conn, need["id"])["eligible_candidates"] == []
    sv.escalate(conn, rid, "Both other pharmacists are on leave")
    req = sv.get_request(conn, rid)
    assert req["status"] == "reported" and req["escalation_reason"]       # absence stays recorded
    assert sv.coverage_summary(conn, rid)["state"] == "escalated"
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE kind='escalation'").fetchone()[0] == 1
    assert sv.verify_coverage(conn, rid)["covered"] is False               # honest about the gap


def test_sick_with_no_staffing_gap_needs_no_coverage(conn):
    rid = sv.create_leave_request(conn, emp(conn, "Amal Al-Jaber"), day(0), leave_type="sick", today=TODAY)
    assert sv.compute_coverage_needs(conn, rid) == []
    assert sv.coverage_summary(conn, rid)["state"] == "no_gap"


def test_sick_partial_day_splits_shift(conn):
    rid = sv.create_leave_request(conn, emp(conn, "Maha Al-Anazi"), day(2), 12, 16, leave_type="sick", today=TODAY)
    rows = [(r["start_hour"], r["end_hour"], r["status"]) for r in conn.execute(
        "SELECT * FROM shifts WHERE employee_id=? AND date=? ORDER BY start_hour", (emp(conn, "Maha Al-Anazi"), day(2)))]
    assert rows == [(8, 12, "scheduled"), (12, 16, "on_leave")]


def test_sick_agent_cannot_route_to_manager(conn):
    rid = sick_hessa(conn)
    client = StubClient([
        turn(text("Assessing impact."), tool("analyze_staffing")),
        turn(tool("submit_to_manager", summary="please approve")),
        turn(tool("send_coverage_offer", need_id=1, candidate_id=emp(conn, "Tariq Al-Subaie"), rationale="free")),
        turn(stop="end_turn"),
    ])
    agent.run_agent(conn, rid, "sick_reported", client)
    blocked = [r for r in conn.execute("SELECT message FROM audit_log WHERE event_type='safeguard_blocked'")]
    assert len(blocked) == 1 and "submit_to_manager" in blocked[0]["message"]
    assert conn.execute("SELECT status FROM coverage_offers").fetchone()[0] == "pending"


# ---------------------------------------------------------------- compatibility
def test_old_database_is_migrated(tmp_path):
    c = db.connect(str(tmp_path / "old.db"))
    c.execute("CREATE TABLE leave_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, date TEXT, "
              "start_hour INTEGER, end_hour INTEGER, leave_type TEXT, reason TEXT, leave_hours REAL, status TEXT, "
              "manager_id INTEGER, escalation_reason TEXT, manager_comment TEXT, created_at TEXT, decided_at TEXT, "
              "verified INTEGER, verification_json TEXT)")
    c.execute("INSERT INTO leave_requests(employee_id,date,start_hour,end_hour,leave_type,leave_hours,status,created_at) "
              "VALUES (1,'2026-11-04',0,24,'annual',8,'approved','x')")
    db.init_db(c)
    assert c.execute("SELECT end_date FROM leave_requests").fetchone()[0] == "2026-11-04"
    db.init_db(c)                                                          # idempotent
