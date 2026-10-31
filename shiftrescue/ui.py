"""Read-only presentation helpers for the Nasaq UI.
Nothing here writes to the database or changes business rules; everything is derived
from existing tables (leave_requests, coverage_needs/offers, shifts, audit_log, notifications)."""
from __future__ import annotations

import json
from datetime import date as _date, timedelta

from . import rules, services as sv

# ------------------------------------------------------------------ labels
STATUS_LABEL = {
    "submitted": ("Submitted", "info"),
    "seeking_coverage": ("Finding coverage", "info"),
    "awaiting_manager": ("Awaiting manager", "warn"),
    "escalated": ("Escalated", "danger"),
    "approved": ("Annual leave approved", "success"),
    "rejected": ("Declined by manager", "neutral"),
    "reported": ("Sick absence reported", "sick"),
}

NOTIFICATION_LABEL = {
    "approval_needed": "Approval requested",
    "escalation": "Escalation",
    "agent_update": "Update from Nasaq",
    "coverage_offer": "Coverage offer",
    "shift_confirmed": "Shift confirmed",
    "approved": "Leave approved",
    "rejected": "Leave declined",
    "status": "Status update",
    "sick_reported": "Sick absence reported",
    "sick_coverage": "Sick absence coverage",
}


def status_label(req: dict) -> tuple[str, str]:
    return STATUS_LABEL.get(req["status"], (req["status"], "neutral"))


def leave_kind(req: dict) -> str:
    return "Sick absence" if req["leave_type"] == "sick" else "Annual leave"


def short_period(req: dict) -> str:
    d0 = _date.fromisoformat(req["date"])
    d1 = _date.fromisoformat(req.get("end_date") or req["date"])
    if d0 == d1:
        base = d0.strftime("%a %d %b")
        if (req["start_hour"], req["end_hour"]) != (0, 24):
            base += f", {req['start_hour']:02d}:00-{req['end_hour']:02d}:00"
        return base
    return f"{d0.strftime('%a %d %b')} - {d1.strftime('%a %d %b')} ({(d1 - d0).days + 1} days)"


def fmt_ts(ts: str) -> str:
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts).strftime("%d %b, %H:%M")
    except Exception:
        return ts


# ------------------------------------------------------------------ requests
def visible_requests(conn) -> list[dict]:
    """Requests submitted through the app (excludes pre-seeded background leave)."""
    rows = conn.execute(
        "SELECT r.*, e.name AS employee, e.role AS employee_role FROM leave_requests r "
        "JOIN employees e ON e.id=r.employee_id "
        "WHERE r.id IN (SELECT request_id FROM audit_log WHERE event_type IN ('request_submitted','sick_reported')) "
        "ORDER BY r.id DESC").fetchall()
    return [dict(r) for r in rows]


def request_label(r: dict) -> str:
    return f"#{r['id']} · {r['employee']} · {short_period(r)} · {leave_kind(r)}"


def next_step(conn, r: dict) -> str:
    """One-line, factual 'what happens next' derived from stored state."""
    cs = sv.coverage_summary(conn, r["id"])
    st = r["status"]
    pending = conn.execute("SELECT COUNT(*) FROM coverage_offers WHERE request_id=? AND status='pending'", (r["id"],)).fetchone()[0]
    if st == "approved":
        v = r.get("verified")
        return "Schedule updated; coverage verified." if v == 1 else ("Schedule updated; coverage gaps remain." if v == 0 else "Schedule updated.")
    if st == "rejected":
        return "Declined by your manager. Your schedule is unchanged."
    if st == "awaiting_manager":
        return "Coverage secured. Waiting for manager approval."
    if st == "escalated":
        return "No replacement found for some shifts. Escalated to your manager (not denied)."
    if st == "reported":
        if cs["state"] == "no_gap":
            return "Recorded. Minimum staffing is still met, no cover needed."
        if cs["state"] == "covered":
            return "Recorded. All affected shifts are covered."
        if cs["state"] == "escalated":
            return "Recorded. A staffing shortage was escalated to your manager."
        if cs["state"] == "not_analyzed":
            return "Recorded. Nasaq is assessing the staffing impact."
        return f"Recorded. Arranging cover ({cs['covered']}/{cs['total']} shifts covered)."
    if pending:
        return f"Waiting for {pending} replacement response(s)."
    if cs["state"] == "not_analyzed":
        return "Nasaq is analyzing staffing impact."
    return f"Finding coverage ({cs['covered']}/{cs['total']} shifts covered)."


# ------------------------------------------------------------------ staffing / calendar
def _ranges(hours: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for h in sorted(hours):
        if out and out[-1][1] == h:
            out[-1] = (out[-1][0], h + 1)
        else:
            out.append((h, h + 1))
    return out


def staffing_gaps(conn, date: str) -> dict[str, list[tuple[int, int]]]:
    """Per role: hour ranges (within hours that role is rostered) below minimum staffing."""
    out: dict[str, list[tuple[int, int]]] = {}
    for r in conn.execute("SELECT role, min_staff FROM staffing_minimums ORDER BY role").fetchall():
        hours: set[int] = set()
        for s in conn.execute("SELECT start_hour,end_hour FROM shifts WHERE role=? AND date=? AND status IN ('scheduled','on_leave')",
                              (r["role"], date)):
            hours |= set(range(s["start_hour"], s["end_hour"]))
        bad = [h for h in hours if rules.staffing_count(conn, r["role"], date, h) < r["min_staff"]]
        out[r["role"]] = _ranges(bad)
    return out


def date_span(start: _date, days: int) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range(days)]


def calendar_cells(conn, dates: list[str], role: str | None = None):
    """Returns (employees, cells) where cells[(emp_id, date)] = [(css_class, label), ...]."""
    q = "SELECT id,name,role FROM employees WHERE active=1 AND is_manager=0"
    args: list = []
    if role:
        q += " AND role=?"
        args.append(role)
    emps = [dict(e) for e in conn.execute(q + " ORDER BY role,name", args)]
    cells: dict[tuple[int, str], list[tuple[str, str]]] = {}
    ph = ",".join("?" * len(dates))
    for s in conn.execute(
            f"SELECT s.*, r.leave_type FROM shifts s LEFT JOIN leave_requests r ON r.id=s.leave_request_id "
            f"WHERE s.date IN ({ph}) ORDER BY s.start_hour", dates):
        rng = f"{s['start_hour']:02d}-{s['end_hour']:02d}"
        if s["status"] == "on_leave":
            cls, label = ("sick", f"Sick {rng}") if s["leave_type"] == "sick" else ("leave", f"Leave {rng}")
        elif s["source"] == "coverage":
            cls, label = "cover", f"Cover {rng}"
        else:
            cls, label = "shift", rng
        cells.setdefault((s["employee_id"], s["date"]), []).append((cls, label))
    # leave requested but not yet applied to the schedule
    for r in conn.execute("SELECT * FROM leave_requests WHERE leave_type='annual' AND status IN "
                          "('submitted','seeking_coverage','awaiting_manager','escalated')"):
        for d in sv.req_dates(dict(r)):
            if d in dates:
                cells.setdefault((r["employee_id"], d), []).append(("pending", "Leave requested"))
    return emps, cells


# ------------------------------------------------------------------ AI activity timeline
_TIMELINE_EVENTS = {
    "request_submitted": ("Leave request received", "info"),
    "sick_reported": ("Sick absence reported", "sick"),
    "staffing_analyzed": ("Policy and staffing analyzed", "info"),
    "offer_sent": ("Coverage offer sent", "info"),
    "offer_accepted": ("Coverage offer accepted", "success"),
    "offer_declined": ("Coverage offer declined", "warn"),
    "routed_to_manager": ("Sent to manager for review", "warn"),
    "escalated": ("Escalated to manager", "danger"),
    "manager_approved": ("Manager approved", "success"),
    "manager_rejected": ("Manager declined", "neutral"),
    "schedule_updated": ("Schedule updated", "success"),
    "approval_blocked": ("Approval paused", "warn"),
    "safeguard_blocked": ("Safeguard blocked an action", "warn"),
    "agent_error": ("Agent could not complete", "danger"),
}


def timeline(conn, request_id: int) -> list[dict]:
    """Concise milestone list derived only from audit_log (+ need rows for names/dates)."""
    steps: list[dict] = []
    searched: set[int] = set()
    for ev in conn.execute("SELECT * FROM audit_log WHERE request_id=? ORDER BY id", (request_id,)):
        et, msg = ev["event_type"], ev["message"]
        payload = json.loads(ev["payload"]) if ev["payload"] else {}
        step = None
        if et == "tool_call" and payload.get("tool") == "find_candidates":
            nid = (payload.get("args") or {}).get("need_id")
            need = conn.execute("SELECT * FROM coverage_needs WHERE id=?", (nid,)).fetchone()
            where = f"{need['role']} · {need['date']} {need['start_hour']:02d}:00-{need['end_hour']:02d}:00" if need else f"need {nid}"
            again = nid in searched
            searched.add(nid)
            step = {"title": "Searched again for the next candidate" if again else "Qualified replacements identified",
                    "detail": where, "tone": "info"}
        elif et == "coverage_verified":
            ok = bool(payload.get("covered"))
            gaps = payload.get("gaps") or []
            step = {"title": "Coverage verified" if ok else "Coverage verification found gaps",
                    "detail": "Minimum staffing met for every affected hour." if ok else
                    "Below minimum: " + "; ".join(f"{g['date']} {g['hour']:02d}:00 ({g['role']})" for g in gaps[:6]),
                    "tone": "success" if ok else "danger"}
        elif et in _TIMELINE_EVENTS:
            title, tone = _TIMELINE_EVENTS[et]
            detail = msg
            if et == "offer_sent" and payload.get("rationale"):
                detail += f" - rationale: {payload['rationale']}"
            if et == "schedule_updated":
                detail = ("Leave record, balance and shifts updated together. "
                          + ("Coverage verified." if payload.get("covered") else "Some hours remain uncovered."))
            step = {"title": title, "detail": detail, "tone": tone}
        if step:
            step.update(ts=ev["ts"], actor=ev["actor"])
            steps.append(step)
    return steps


def raw_events(conn, request_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, ts, actor, event_type, message, payload FROM audit_log WHERE request_id=? ORDER BY id", (request_id,))]
