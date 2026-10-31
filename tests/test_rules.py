"""Scheduling constraint tests (deterministic safeguards)."""
import pytest

from conftest import TODAY, day, emp
from shiftrescue import rules, scenarios, services as sv


def nurse_request(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    needs = sv.compute_coverage_needs(conn, rid)
    return rid, needs[0]


def test_gap_detected_for_nurse_full_day(conn):
    rid, need = nurse_request(conn)
    assert (need["role"], need["start_hour"], need["end_hour"]) == ("Nurse", 7, 15)


def test_no_gap_when_minimum_still_met(conn):
    rid = sv.create_leave_request(conn, emp(conn, "Amal Al-Jaber"), day(0), today=TODAY)  # receptionist, min 1, 2 on shift
    assert sv.compute_coverage_needs(conn, rid) == []
    assert sv.get_request(conn, rid)["status"] == "seeking_coverage"


def test_rest_rule_blocks_evening_worker_for_day_shift(conn):
    rid, need = nurse_request(conn)
    # Faisal... evening nurses on Tuesday (idx 6,7) finish 23:00 -> only 8h rest before 07:00
    huda = emp(conn, "Huda Al-Zahrani")
    reasons = rules.check_candidate(conn, need, huda)
    assert any("rest rule" in r for r in reasons)


def test_qualification_enforced(conn):
    rid, need = nurse_request(conn)
    reasons = rules.check_candidate(conn, need, emp(conn, "Amal Al-Jaber"))
    assert any("not qualified" in r for r in reasons)


def test_already_working_blocks(conn):
    rid, need = nurse_request(conn)
    reasons = rules.check_candidate(conn, need, emp(conn, "Faisal Al-Shehri"))  # works same shift
    assert any("already working" in r for r in reasons)


def test_weekly_cap_enforced(conn):
    rid, need = nurse_request(conn)
    nora = emp(conn, "Nora Al-Dosari")
    assert rules.check_candidate(conn, need, nora) == []
    conn.execute("UPDATE employees SET max_weekly_hours=8 WHERE id=?", (nora,))
    assert any("weekly hours cap" in r for r in rules.check_candidate(conn, need, nora))


def test_approved_leave_blocks_candidate(conn):
    rid, need = nurse_request(conn)
    nora = emp(conn, "Nora Al-Dosari")
    conn.execute("INSERT INTO leave_requests(employee_id,date,start_hour,end_hour,leave_type,leave_hours,status,created_at) "
                 "VALUES (?,?,0,24,'annual',0,'approved','x')", (nora, need["date"]))
    assert "on approved leave" in rules.check_candidate(conn, need, nora)


def test_find_candidates_only_returns_eligible(conn):
    rid, need = nurse_request(conn)
    names = {c["name"] for c in sv.find_candidates(conn, need["id"])["eligible_candidates"]}
    assert names == {"Nora Al-Dosari", "Khalid Al-Mutairi"}


def test_send_offer_refuses_ineligible_candidate(conn):
    rid, need = nurse_request(conn)
    with pytest.raises(sv.ServiceError, match="not eligible"):
        sv.send_offer(conn, need["id"], emp(conn, "Amal Al-Jaber"), "llm tried a receptionist")
    assert conn.execute("SELECT COUNT(*) FROM coverage_offers").fetchone()[0] == 0


def test_only_one_pending_offer_per_need(conn):
    rid, need = nurse_request(conn)
    sv.send_offer(conn, need["id"], emp(conn, "Nora Al-Dosari"))
    with pytest.raises(sv.ServiceError, match="pending"):
        sv.send_offer(conn, need["id"], emp(conn, "Khalid Al-Mutairi"))


def test_same_candidate_not_offered_twice_and_double_booking_blocked(conn):
    rid, need = nurse_request(conn)
    nora = emp(conn, "Nora Al-Dosari")
    oid = sv.send_offer(conn, need["id"], nora)
    sv.respond_to_offer(conn, oid, False)
    with pytest.raises(sv.ServiceError):
        sv.send_offer(conn, need["id"], nora)


def test_insufficient_balance_rejected(conn):
    with pytest.raises(sv.ServiceError, match="insufficient leave balance"):
        sv.create_leave_request(conn, emp(conn, "Bader Al-Harthi"), day(2), today=TODAY)  # 4h balance, 8h shift


def test_sick_leave_ignores_balance(conn):
    rid = sv.create_leave_request(conn, emp(conn, "Bader Al-Harthi"), day(2), leave_type="sick", today=TODAY)
    assert rid


def test_past_date_and_no_shift_rejected(conn):
    with pytest.raises(sv.ServiceError):
        sv.create_leave_request(conn, emp(conn, "Reem Al-Ghamdi"), "2026-10-01", today=TODAY)
    with pytest.raises(sv.ServiceError, match="no scheduled shift"):
        sv.create_leave_request(conn, emp(conn, "Reem Al-Ghamdi"), day(1), today=TODAY)
