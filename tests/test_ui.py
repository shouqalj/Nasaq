"""Presentation-layer tests: helper functions and a headless UI smoke test.
No API calls: the key is blanked, agent.make_client is blocked by conftest, and a temp DB is used."""
import json
from pathlib import Path

import pytest

from conftest import BASE, TODAY, day, emp
from shiftrescue import db, scenarios, seed, services as sv, ui

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- helpers
def test_timeline_is_concise_and_keeps_raw_trail(conn):
    rid = scenarios.create_scenario_request(conn, "decline_then_accept", today=TODAY)
    need = sv.compute_coverage_needs(conn, rid)[0]
    # raw tool-call style events as the agent loop would log them
    sv.audit(conn, rid, "agent", "tool_call", 'find_candidates({"need_id": %d})' % need["id"],
             {"tool": "find_candidates", "args": {"need_id": need["id"]}})
    sv.audit(conn, rid, "agent", "tool_call", "get_policies({})", {"tool": "get_policies", "args": {}})
    sv.audit(conn, rid, "agent", "reasoning", "thinking out loud")
    o = sv.send_offer(conn, need["id"], emp(conn, "Nora Al-Dosari"), "likes extra shifts")
    sv.respond_to_offer(conn, o, False)
    o = sv.send_offer(conn, need["id"], emp(conn, "Khalid Al-Mutairi"), "next best")
    sv.respond_to_offer(conn, o, True)
    sv.submit_to_manager(conn, rid, "ok")
    sv.manager_decide(conn, rid, True)
    titles = [s["title"] for s in ui.timeline(conn, rid)]
    assert titles == ["Leave request received", "Policy and staffing analyzed", "Qualified replacements identified",
                      "Coverage offer sent", "Coverage offer declined", "Coverage offer sent", "Coverage offer accepted",
                      "Sent to manager for review", "Manager approved", "Schedule updated"]
    assert any("Nora Al-Dosari" in s["detail"] for s in ui.timeline(conn, rid))      # employee names shown
    raw_types = {e["event_type"] for e in ui.raw_events(conn, rid)}
    assert {"tool_call", "reasoning"} <= raw_types                                      # nothing deleted


def test_timeline_marks_safeguard_and_escalation(conn):
    rid = scenarios.create_scenario_request(conn, "no_replacement", today=TODAY)
    need = sv.compute_coverage_needs(conn, rid)[0]
    sv.audit(conn, rid, "system", "safeguard_blocked", "send_coverage_offer refused: not eligible")
    sv.escalate(conn, rid, "No pharmacist available")
    steps = ui.timeline(conn, rid)
    assert [s["title"] for s in steps][-2:] == ["Safeguard blocked an action", "Escalated to manager"]
    assert steps[-1]["tone"] == "danger"


def test_status_labels_distinguish_sick_from_annual(conn):
    sick = sv.get_request(conn, scenarios.create_scenario_request(conn, "sick_multi_day", today=TODAY))
    annual = sv.get_request(conn, scenarios.create_scenario_request(conn, "partial_day", today=TODAY))
    assert ui.status_label(sick)[0] == "Sick absence reported"
    assert ui.status_label(annual)[0] == "Submitted"
    assert "approved" not in ui.status_label(sick)[0].lower()
    assert ui.leave_kind(sick) == "Sick absence" and ui.leave_kind(annual) == "Annual leave"


def test_staffing_gaps_flag_uncovered_absence_only(conn):
    assert all(v == [] for v in ui.staffing_gaps(conn, day(0)).values())
    rid = sv.create_leave_request(conn, emp(conn, "Rana Al-Tamimi"), day(0), leave_type="sick", today=TODAY)
    assert ui.staffing_gaps(conn, day(0))["Pharmacist"] == [(9, 18)]
    need = sv.compute_coverage_needs(conn, rid)[0]
    assert need["role"] == "Pharmacist"


def test_calendar_cells_distinguish_shift_leave_sick_and_cover(conn):
    rid = scenarios.create_scenario_request(conn, "partial_day", today=TODAY)
    need = sv.compute_coverage_needs(conn, rid)[0]
    o = sv.send_offer(conn, need["id"], emp(conn, "Tariq Al-Subaie"))
    sv.respond_to_offer(conn, o, True)
    maha, tariq = emp(conn, "Maha Al-Anazi"), emp(conn, "Tariq Al-Subaie")
    _, cells = ui.calendar_cells(conn, [day(2)])
    assert ("pending", "Leave requested") in cells[(maha, day(2))]                      # not yet approved
    sv.submit_to_manager(conn, rid)
    sv.manager_decide(conn, rid, True)
    _, cells = ui.calendar_cells(conn, [day(2)])
    assert {c for c, _ in cells[(maha, day(2))]} == {"shift", "leave"}
    assert ("cover", "Cover 12-16") in cells[(tariq, day(2))]
    sick = sv.create_leave_request(conn, emp(conn, "Hessa Al-Rashid"), day(3), leave_type="sick", today=TODAY)
    _, cells = ui.calendar_cells(conn, [day(3)])
    assert cells[(emp(conn, "Hessa Al-Rashid"), day(3))][0][0] == "sick"


def test_visible_requests_hides_seeded_background_leave(conn):
    assert ui.visible_requests(conn) == []                                              # 2 seeded approved rows exist
    rid = scenarios.create_scenario_request(conn, "sick_multi_day", today=TODAY)
    assert [r["id"] for r in ui.visible_requests(conn)] == [rid]


# ---------------------------------------------------------------- headless UI smoke test
def _tmp_app_db(tmp_path, monkeypatch):
    path = str(tmp_path / "ui.db")
    monkeypatch.setattr(db, "DEFAULT_DB", path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")                                          # no real calls possible
    c = db.connect(path)
    db.init_db(c)
    seed.seed(c, seed.next_monday())
    return c


def _all_text(at):
    out = []
    for kind in ("markdown", "caption", "text", "title", "header", "subheader", "error", "warning", "info", "success"):
        out += [str(getattr(e, "value", "")) for e in getattr(at, kind)]
    for e in at.sidebar.markdown:
        out.append(str(e.value))
    return "\n".join(out)


def test_ui_pages_render_without_errors_or_old_branding(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    c = _tmp_app_db(tmp_path, monkeypatch)
    # state covering every manager-workspace section
    r_pending = scenarios.create_scenario_request(c, "partial_day")                       # -> awaiting manager
    need = sv.compute_coverage_needs(c, r_pending)[0]
    o = sv.send_offer(c, need["id"], emp(c, "Tariq Al-Subaie"))
    sv.respond_to_offer(c, o, True)
    sv.submit_to_manager(c, r_pending)
    scenarios.create_scenario_request(c, "decline_then_accept")                           # ongoing coverage
    r_sick = scenarios.create_scenario_request(c, "no_replacement")                       # annual, will escalate
    sv.compute_coverage_needs(c, r_sick)
    sv.escalate(c, r_sick, "No pharmacist available")
    scenarios.create_scenario_request(c, "sick_multi_day")                                # reported sick
    before = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    for page in ["Overview", "My Leave", "Manager Workspace", "Workforce Calendar", "AI Activity", "Demo Scenarios"]:
        at.sidebar.radio(key="nav").set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])
        assert not [e for e in at.error if "Something went wrong" in str(e.value)], page
        text = _all_text(at)
        assert "ShiftRescue" not in text and "DeltaGenerator" not in text and "🛟" not in text, page
    # manager workspace: approvals and escalations are actionable, history is collapsed
    at.sidebar.radio(key="nav").set_value("Manager Workspace").run()
    assert any(b.label == "Approve" for b in at.button)
    assert "Action required (2)" in _all_text(at)
    assert not any("approval_needed" in str(e.value) for e in at.markdown)               # raw kinds are not shown
    # every scenario button still exists
    at.sidebar.radio(key="nav").set_value("Demo Scenarios").run()
    assert {b.key for b in at.button if b.key and b.key.startswith("sc_")} == {
        f"sc_{k}" for k in scenarios.SCENARIOS}
    # nothing was deleted by merely browsing
    assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


def test_ui_manager_approval_still_goes_through_service(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    c = _tmp_app_db(tmp_path, monkeypatch)
    rid = scenarios.create_scenario_request(c, "partial_day")
    need = sv.compute_coverage_needs(c, rid)[0]
    o = sv.send_offer(c, need["id"], emp(c, "Tariq Al-Subaie"))
    sv.respond_to_offer(c, o, True)
    sv.submit_to_manager(c, rid)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    at.sidebar.radio(key="nav").set_value("Manager Workspace").run()
    [b for b in at.button if b.key == f"ap{rid}"][0].click().run()
    assert not at.exception
    assert sv.get_request(c, rid)["status"] == "approved"
    assert c.execute("SELECT COUNT(*) FROM shifts WHERE source='coverage'").fetchone()[0] == 1


def test_ui_submit_sick_leave_without_api(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    c = _tmp_app_db(tmp_path, monkeypatch)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    at.sidebar.radio(key="nav").set_value("Demo Scenarios").run()
    [b for b in at.button if b.key == "sc_sick_multi_day"][0].click().run()
    assert not at.exception
    row = c.execute("SELECT status, leave_type FROM leave_requests ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["status"], row["leave_type"]) == ("reported", "sick")
    assert any("not configured" in str(w.value) for w in at.warning)                      # friendly warning, no crash
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='agent_error'").fetchone()[0] == 1
