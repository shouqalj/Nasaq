"""Demo scenarios. These only *submit the leave request*; everything after is decided by the agent."""
from __future__ import annotations

from datetime import date, timedelta

from . import services as sv

SCENARIOS = {
    "decline_then_accept": dict(
        title="1. First replacement declines",
        blurb="Nurse requests a full day off. Decline the first offer and watch the agent find another candidate.",
        employee="Reem Al-Ghamdi", day=2, start=None, end=None, leave_type="annual", reason="Family event"),
    "partial_day": dict(
        title="2. Partial-day leave, approval & schedule update",
        blurb="Lab technician needs the afternoon off. Accept the offer, approve as manager, inspect the schedule.",
        employee="Maha Al-Anazi", day=2, start=12, end=16, leave_type="annual", reason="Medical appointment"),
    "no_replacement": dict(
        title="3. No qualified replacement",
        blurb="The only pharmacist on shift requests leave while both colleagues are away. Expect escalation, not denial.",
        employee="Rana Al-Tamimi", day=0, start=None, end=None, leave_type="annual", reason="Personal matter"),
    "multi_day": dict(
        title="4. Multi-day annual leave (Mon-Fri)",
        blurb="Nurse requests 5 calendar days but works only 3 shifts (24h deducted). Different replacements are needed on "
              "Mon, Wed and Fri; the Friday pool is tiny (rest/hours rules), so expect declines or partial coverage to be interesting.",
        employee="Layla Al-Harbi", day=0, end_day=4, start=None, end=None, leave_type="annual", reason="Family trip"),
    "sick_multi_day": dict(
        title="5. Sick leave, 2 days (Thu-Fri)",
        blurb="Lab technician reports sick. The absence is Reported instantly (no approval, no balance deduction); "
              "the agent then finds cover for each day independently.",
        employee="Hessa Al-Rashid", day=3, end_day=4, start=None, end=None, leave_type="sick", reason="Flu"),
}


def create_scenario_request(conn, key, today=None):
    s = SCENARIOS[key]
    emp = conn.execute("SELECT id FROM employees WHERE name=?", (s["employee"],)).fetchone()
    base = date.fromisoformat(conn.execute("SELECT value FROM meta WHERE key='base_monday'").fetchone()[0])
    d = (base + timedelta(days=s["day"])).isoformat()
    end = (base + timedelta(days=s.get("end_day", s["day"]))).isoformat()
    return sv.create_leave_request(conn, emp["id"], d, s["start"], s["end"], s["leave_type"], s["reason"],
                                   today=today, end_date=end)
