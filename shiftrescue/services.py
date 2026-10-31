"""Workflow services. Every state change goes through here, and every function
re-validates against shiftrescue.rules - the agent has no other way to write data."""
from __future__ import annotations

import json
from datetime import date as _date, timedelta

from . import rules
from .db import now, tx


class ServiceError(Exception):
    """A deterministic safeguard refused the action."""


MAX_LEAVE_DAYS = 7


def _dates(start, end=None):
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end or start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def req_dates(req):
    return _dates(req["date"], req.get("end_date"))


def period_text(req):
    same = not req.get("end_date") or req["end_date"] == req["date"]
    d = req["date"] if same else f"{req['date']} to {req['end_date']}"
    hrs = "full day(s)" if (req["start_hour"], req["end_hour"]) == (0, 24) else f"{req['start_hour']}:00-{req['end_hour']}:00"
    return f"{d} ({hrs})"


# ---------- helpers ----------
def _one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def _all(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def audit(conn, request_id, actor, event_type, message, payload=None):
    conn.execute(
        "INSERT INTO audit_log(ts,request_id,actor,event_type,message,payload) VALUES (?,?,?,?,?,?)",
        (now(), request_id, actor, event_type, message, json.dumps(payload, default=str) if payload is not None else None))


def notify(conn, recipient_id, request_id, kind, message):
    conn.execute("INSERT INTO notifications(recipient_id,request_id,kind,message,created_at) VALUES (?,?,?,?,?)",
                 (recipient_id, request_id, kind, message, now()))


def get_request(conn, request_id):
    r = _one(conn, "SELECT * FROM leave_requests WHERE id=?", (request_id,))
    if not r:
        raise ServiceError(f"request {request_id} not found")
    return r


def get_need(conn, need_id, request_id=None):
    n = _one(conn, "SELECT * FROM coverage_needs WHERE id=?", (need_id,))
    if not n or (request_id is not None and n["request_id"] != request_id):
        raise ServiceError(f"coverage need {need_id} not found for this request")
    return n


def _name(conn, emp_id):
    r = _one(conn, "SELECT name FROM employees WHERE id=?", (emp_id,))
    return r["name"] if r else str(emp_id)


def _manager_id(conn):
    r = _one(conn, "SELECT id FROM employees WHERE is_manager=1 AND active=1 ORDER BY id LIMIT 1")
    return r["id"] if r else None


# ---------- 1. submit ----------
def create_leave_request(conn, employee_id, date, start_hour=None, end_hour=None,
                         leave_type="annual", reason="", today=None, end_date=None):
    """start_hour/end_hour None == full day. end_date None == single day (max 7 calendar days).
    Annual leave starts as 'submitted'. Sick leave is recorded immediately as 'reported' and
    its shifts are marked absent at once; it never waits for approval or coverage."""
    today = today or _date.today()
    end_date = end_date or date
    emp = _one(conn, "SELECT * FROM employees WHERE id=? AND active=1", (employee_id,))
    if not emp:
        raise ServiceError("employee not found")
    if leave_type not in ("annual", "sick"):
        raise ServiceError("leave_type must be 'annual' or 'sick'")
    d0, d1 = _date.fromisoformat(date), _date.fromisoformat(end_date)
    if d1 < d0:
        raise ServiceError("end date cannot be before start date")
    if (d1 - d0).days + 1 > MAX_LEAVE_DAYS:
        raise ServiceError(f"leave can span at most {MAX_LEAVE_DAYS} consecutive calendar days")
    sick = leave_type == "sick"
    if sick and d0 < today:
        raise ServiceError("sick leave can only be reported for today or a future date")
    if not sick and d0 <= today:
        raise ServiceError("leave must be requested for a future date")
    ls, le = (0, 24) if start_hour is None else (int(start_hour), int(end_hour))
    if not (0 <= ls < le <= 24):
        raise ServiceError("invalid hour range")
    if d1 > d0 and (ls, le) != (0, 24):
        raise ServiceError("multi-day leave must cover full days (hourly leave is single-day only)")
    hours = sum(max(0, min(s["end_hour"], le) - max(s["start_hour"], ls))
                for day in _dates(date, end_date) for s in rules.shifts_on(conn, employee_id, day))
    if hours <= 0:
        raise ServiceError("you have no scheduled shift in that period")
    dup = _one(conn, "SELECT id FROM leave_requests WHERE employee_id=? AND date<=? AND COALESCE(end_date,date)>=? "
                     "AND start_hour<? AND end_hour>? "
                     "AND status IN ('submitted','seeking_coverage','awaiting_manager','escalated','approved','reported')",
               (employee_id, end_date, date, le, ls))
    if dup:
        raise ServiceError(f"overlaps existing leave request #{dup['id']}")
    if leave_type == "annual" and emp["leave_balance_hours"] < hours:
        raise ServiceError(f"insufficient leave balance: {emp['leave_balance_hours']:g}h available, {hours:g}h needed")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO leave_requests(employee_id,date,end_date,start_hour,end_hour,leave_type,reason,leave_hours,status,manager_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (employee_id, date, end_date, ls, le, leave_type, reason, hours,
             "reported" if sick else "submitted", _manager_id(conn), now()))
        rid = cur.lastrowid
        req = get_request(conn, rid)
        if sick:
            _apply_leave_to_schedule(conn, req)          # absence recorded immediately; no balance impact
            audit(conn, rid, "system", "sick_reported",
                  f"{emp['name']} reported sick: {period_text(req)}, {hours:g} scheduled hours. Recorded as Reported (no approval needed).")
            if req["manager_id"]:
                notify(conn, req["manager_id"], rid, "sick_reported",
                       f"{emp['name']} reported sick {period_text(req)}. Coverage assessment is starting.")
            notify(conn, employee_id, rid, "status", "Your sick absence has been recorded as Reported. Get well soon.")
        else:
            audit(conn, rid, "system", "request_submitted",
                  f"{emp['name']} requested annual leave {period_text(req)}, {hours:g} scheduled hours")
    return rid


# ---------- 2. staffing analysis ----------
def _leave_segments(conn, req):
    """(shift, from_hour, to_hour) for every shift segment the leave removes from the schedule."""
    if req["leave_type"] == "sick":   # already applied to the schedule at report time
        for sh in _all(conn, "SELECT * FROM shifts WHERE leave_request_id=? AND status='on_leave' ORDER BY date,start_hour", (req["id"],)):
            yield sh, sh["start_hour"], sh["end_hour"]
        return
    for day in req_dates(req):
        for sh in rules.shifts_on(conn, req["employee_id"], day):
            a, b = max(sh["start_hour"], req["start_hour"]), min(sh["end_hour"], req["end_hour"])
            if a < b:
                yield dict(sh), a, b


def compute_coverage_needs(conn, request_id):
    """Deterministically find affected shifts (across all days) and the hours that would breach
    minimum staffing. Idempotent."""
    req = get_request(conn, request_id)
    done = conn.execute("SELECT 1 FROM audit_log WHERE request_id=? AND event_type='staffing_analyzed'", (request_id,)).fetchone()
    if done or req["status"] not in ("submitted", "reported"):
        return describe_needs(conn, request_id)
    with tx(conn):
        for sh, a, b in list(_leave_segments(conn, req)):
            gaps = rules.coverage_gap_hours(conn, sh["role"], sh["date"], a, b, req["employee_id"])
            if gaps:
                conn.execute(
                    "INSERT INTO coverage_needs(request_id,shift_id,role,date,start_hour,end_hour) VALUES (?,?,?,?,?,?)",
                    (request_id, sh["id"], sh["role"], sh["date"], min(gaps), max(gaps) + 1))
        if req["leave_type"] != "sick":
            conn.execute("UPDATE leave_requests SET status='seeking_coverage' WHERE id=?", (request_id,))
        n = conn.execute("SELECT COUNT(*) FROM coverage_needs WHERE request_id=?", (request_id,)).fetchone()[0]
        audit(conn, request_id, "system", "staffing_analyzed",
              f"{n} shift segment(s) fall below minimum staffing" if n else "Minimum staffing stays satisfied - no replacement required")
    return describe_needs(conn, request_id)


def coverage_summary(conn, request_id):
    needs = describe_needs(conn, request_id)
    analyzed = conn.execute("SELECT 1 FROM audit_log WHERE request_id=? AND event_type='staffing_analyzed'", (request_id,)).fetchone()
    covered = sum(n["status"] == "covered" for n in needs)
    if not analyzed:
        state = "not_analyzed"
    elif not needs:
        state = "no_gap"
    elif any(n["status"] == "uncovered" for n in needs):
        state = "escalated"
    elif covered == len(needs):
        state = "covered"
    else:
        state = "seeking"
    return {"state": state, "covered": covered, "total": len(needs), "needs": needs}


def describe_needs(conn, request_id):
    needs = _all(conn, "SELECT * FROM coverage_needs WHERE request_id=? ORDER BY id", (request_id,))
    for n in needs:
        n["min_staff_required"] = rules.min_staff(conn, n["role"])
        n["offers"] = _all(conn, "SELECT id,candidate_id,status,rationale FROM coverage_offers WHERE need_id=? ORDER BY id", (n["id"],))
        for o in n["offers"]:
            o["candidate_name"] = _name(conn, o["candidate_id"])
    return needs


# ---------- 3. candidates / offers ----------
def find_candidates(conn, need_id, request_id=None):
    need = get_need(conn, need_id, request_id)
    eligible, ineligible = [], []
    for e in _all(conn, "SELECT * FROM employees WHERE active=1 AND is_manager=0 ORDER BY id"):
        reasons = rules.check_candidate(conn, need, e["id"])
        if reasons:
            if e["role"] == need["role"] or "qualified" not in " ".join(reasons):
                ineligible.append({"employee_id": e["id"], "name": e["name"], "reasons": reasons})
        else:
            eligible.append({
                "employee_id": e["id"], "name": e["name"], "qualifications": e["qualifications"],
                "hours_this_week": rules.week_hours(conn, e["id"], need["date"]),
                "weekly_cap": e["max_weekly_hours"], "seniority_years": e["seniority_years"],
                "prefers_extra_shifts": bool(e["prefers_extra_shifts"])})
    eligible.sort(key=lambda c: (not c["prefers_extra_shifts"], c["hours_this_week"], -c["seniority_years"]))
    sent = conn.execute("SELECT COUNT(*) FROM coverage_offers WHERE need_id=?", (need_id,)).fetchone()[0]
    return {"need": need, "eligible_candidates": eligible, "ineligible_same_role_or_conflicts": ineligible,
            "offers_sent": sent, "offers_remaining": max(0, rules.MAX_OFFERS_PER_NEED - sent)}


def send_offer(conn, need_id, candidate_id, rationale="", request_id=None, actor="agent"):
    need = get_need(conn, need_id, request_id)
    req = get_request(conn, need["request_id"])
    if req["status"] not in rules.COVERAGE_OPEN:
        raise ServiceError(f"request is '{req['status']}', offers can only be sent while seeking coverage")
    if need["status"] == "covered":
        raise ServiceError("this need is already covered")
    if conn.execute("SELECT 1 FROM coverage_offers WHERE need_id=? AND status='pending'", (need_id,)).fetchone():
        raise ServiceError("an offer for this need is still pending - wait for the response")
    sent = conn.execute("SELECT COUNT(*) FROM coverage_offers WHERE need_id=?", (need_id,)).fetchone()[0]
    if sent >= rules.MAX_OFFERS_PER_NEED:
        raise ServiceError(f"offer limit ({rules.MAX_OFFERS_PER_NEED}) reached - escalate to the manager")
    reasons = rules.check_candidate(conn, need, candidate_id)
    if reasons:
        raise ServiceError("candidate is not eligible: " + "; ".join(reasons))
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO coverage_offers(need_id,request_id,candidate_id,status,rationale,created_at) VALUES (?,?,?,?,?,?)",
            (need_id, need["request_id"], candidate_id, "pending", rationale, now()))
        conn.execute("UPDATE coverage_needs SET status='offered' WHERE id=?", (need_id,))
        notify(conn, candidate_id, need["request_id"], "coverage_offer",
               f"Can you cover a {need['role']} shift on {need['date']} {need['start_hour']}:00-{need['end_hour']}:00?")
        audit(conn, need["request_id"], actor, "offer_sent",
              f"Offer sent to {_name(conn, candidate_id)} for {need['date']} {need['start_hour']}:00-{need['end_hour']}:00",
              {"offer_id": cur.lastrowid, "rationale": rationale})
    return cur.lastrowid


def respond_to_offer(conn, offer_id, accept, note="", actor="employee_sim"):
    offer = _one(conn, "SELECT * FROM coverage_offers WHERE id=?", (offer_id,))
    if not offer:
        raise ServiceError("offer not found")
    if offer["status"] != "pending":
        raise ServiceError(f"offer already {offer['status']}")
    need = get_need(conn, offer["need_id"])
    req = get_request(conn, offer["request_id"])
    if req["status"] not in rules.COVERAGE_OPEN:
        raise ServiceError(f"request is '{req['status']}' - offer can no longer be answered")
    cname = _name(conn, offer["candidate_id"])
    if accept:
        reasons = rules.check_candidate(conn, need, offer["candidate_id"], ignore_offer_id=offer_id)
        if reasons:
            raise ServiceError("cannot accept, constraints changed: " + "; ".join(reasons))
    with tx(conn):
        conn.execute("UPDATE coverage_offers SET status=?, responded_at=?, responded_by=?, response_note=? WHERE id=?",
                     ("accepted" if accept else "declined", now(), actor, note, offer_id))
        conn.execute("UPDATE coverage_needs SET status=? WHERE id=?", ("covered" if accept else "open", need["id"]))
        if accept and req["leave_type"] == "sick":
            # No approval step for sick absences: the replacement shift is written right away
            _write_replacement_shift(conn, req, need, offer)
            left = conn.execute("SELECT COUNT(*) FROM coverage_needs WHERE request_id=? AND status<>'covered'", (req["id"],)).fetchone()[0]
            if req["manager_id"]:
                notify(conn, req["manager_id"], req["id"], "sick_coverage",
                       f"Sick absence #{req['id']}: {cname} will cover {need['role']} {need['date']} "
                       f"{need['start_hour']}:00-{need['end_hour']}:00. "
                       + ("All shifts covered." if not left else f"{left} shift segment(s) still need coverage."))
        audit(conn, req["id"], actor, "offer_accepted" if accept else "offer_declined",
              f"{cname} {'ACCEPTED' if accept else 'DECLINED'} the offer (simulated response)" + (f": {note}" if note else ""),
              {"offer_id": offer_id})
    return {"offer_id": offer_id, "status": "accepted" if accept else "declined", "request_id": req["id"]}


# ---------- 4. routing ----------
def submit_to_manager(conn, request_id, summary="", actor="agent"):
    req = get_request(conn, request_id)
    if req["leave_type"] == "sick":
        raise ServiceError("sick absences are recorded immediately and need no manager approval; "
                           "find coverage, or escalate a staffing shortage instead")
    if req["status"] != "seeking_coverage":
        raise ServiceError(f"request is '{req['status']}', cannot route to manager")
    open_needs = _all(conn, "SELECT id FROM coverage_needs WHERE request_id=? AND status<>'covered'", (request_id,))
    if open_needs:
        raise ServiceError(f"coverage not secured for need(s) {[n['id'] for n in open_needs]}; "
                           "find a replacement or escalate")
    with tx(conn):
        conn.execute("UPDATE leave_requests SET status='awaiting_manager' WHERE id=?", (request_id,))
        if req["manager_id"]:
            notify(conn, req["manager_id"], request_id, "approval_needed",
                   f"Leave request #{request_id} for {_name(conn, req['employee_id'])} needs your approval. {summary}")
        notify(conn, req["employee_id"], request_id, "status", "Coverage secured - your request is with your manager.")
        audit(conn, request_id, actor, "routed_to_manager", f"Coverage secured; routed to manager. {summary}")


def escalate(conn, request_id, reason, actor="agent"):
    req = get_request(conn, request_id)
    if req["status"] not in rules.COVERAGE_OPEN:
        raise ServiceError(f"request is '{req['status']}', cannot escalate")
    needs = _all(conn, "SELECT * FROM coverage_needs WHERE request_id=? AND status<>'covered'", (request_id,))
    if not needs:
        raise ServiceError("all needs are covered - route to manager instead")
    for n in needs:
        if conn.execute("SELECT 1 FROM coverage_offers WHERE need_id=? AND status='pending'", (n["id"],)).fetchone():
            raise ServiceError(f"offer pending for need {n['id']} - wait for the response")
        sent = conn.execute("SELECT COUNT(*) FROM coverage_offers WHERE need_id=?", (n["id"],)).fetchone()[0]
        if sent < rules.MAX_OFFERS_PER_NEED and find_candidates(conn, n["id"])["eligible_candidates"]:
            raise ServiceError(f"need {n['id']} still has eligible candidates - keep searching")
    with tx(conn):
        conn.execute("UPDATE coverage_needs SET status='uncovered' WHERE request_id=? AND status<>'covered'", (request_id,))
        uncovered = ", ".join(f"{n['role']} {n['date']} {n['start_hour']}:00-{n['end_hour']}:00" for n in needs)
        if req["leave_type"] == "sick":     # absence stays Reported; only the staffing shortage is escalated
            conn.execute("UPDATE leave_requests SET escalation_reason=? WHERE id=?", (reason, request_id))
        else:
            conn.execute("UPDATE leave_requests SET status='escalated', escalation_reason=? WHERE id=?", (reason, request_id))
        if req["manager_id"]:
            notify(conn, req["manager_id"], request_id, "escalation",
                   f"ESCALATION - request #{request_id}: no replacement available for: {uncovered}. {reason}")
        notify(conn, req["employee_id"], request_id, "status",
               "No replacement found; a staffing shortage was escalated to your manager. "
               + ("Your sick absence remains recorded." if req["leave_type"] == "sick" else "Your request has NOT been denied."))
        audit(conn, request_id, actor, "escalated", f"Escalated to manager (leave not denied/blocked). Uncovered: {uncovered}. {reason}")


# ---------- 5. manager decision + atomic apply ----------
def _write_replacement_shift(conn, req, need, offer):
    src = conn.execute("SELECT * FROM shifts WHERE id=?", (need["shift_id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO shifts(employee_id,date,start_hour,end_hour,role,location,status,source,covering_for,leave_request_id) "
        "VALUES (?,?,?,?,?,?,'scheduled','coverage',?,?)",
        (offer["candidate_id"], need["date"], need["start_hour"], need["end_hour"], need["role"],
         src["location"], need["shift_id"], req["id"]))
    _log_change(conn, req["id"], "replacement_added", cur.lastrowid, offer["candidate_id"], None,
                {"date": need["date"], "start": need["start_hour"], "end": need["end_hour"]})
    notify(conn, offer["candidate_id"], req["id"], "shift_confirmed",
           f"Confirmed: you cover {need['role']} on {need['date']} {need['start_hour']}:00-{need['end_hour']}:00.")


def _log_change(conn, request_id, ctype, shift_id, emp_id, before, after):
    conn.execute(
        "INSERT INTO schedule_changes(request_id,change_type,shift_id,employee_id,before_json,after_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (request_id, ctype, shift_id, emp_id, json.dumps(before) if before else None,
         json.dumps(after) if after else None, now()))


def _apply_leave_to_schedule(conn, req):
    """Mark the employee's shifts in the leave period as on_leave (splitting partial shifts).
    Runs INSIDE the caller's transaction."""
    for day in req_dates(req):
        for s in rules.shifts_on(conn, req["employee_id"], day):
            a, b = max(s["start_hour"], req["start_hour"]), min(s["end_hour"], req["end_hour"])
            if a >= b:
                continue
            conn.execute("UPDATE shifts SET start_hour=?, end_hour=?, status='on_leave', leave_request_id=? WHERE id=?",
                         (a, b, req["id"], s["id"]))
            _log_change(conn, req["id"], "shift_on_leave", s["id"], req["employee_id"],
                        {"start": s["start_hour"], "end": s["end_hour"]}, {"start": a, "end": b, "status": "on_leave"})
            for (rs, re_) in ((s["start_hour"], a), (b, s["end_hour"])):
                if rs < re_:
                    cur = conn.execute(
                        "INSERT INTO shifts(employee_id,date,start_hour,end_hour,role,location,status,source,leave_request_id) "
                        "VALUES (?,?,?,?,?,?,'scheduled','split',?)",
                        (req["employee_id"], s["date"], rs, re_, s["role"], s["location"], req["id"]))
                    _log_change(conn, req["id"], "shift_remainder", cur.lastrowid, req["employee_id"], None, {"start": rs, "end": re_})


def _apply_approved(conn, req, needs):
    """Runs INSIDE the caller's transaction. Any exception rolls everything back."""
    emp = _one(conn, "SELECT * FROM employees WHERE id=?", (req["employee_id"],))
    conn.execute("UPDATE leave_requests SET status='approved', decided_at=? WHERE id=?", (now(), req["id"]))
    if req["leave_type"] == "annual":
        if emp["leave_balance_hours"] < req["leave_hours"]:
            raise ServiceError("insufficient leave balance at approval time")
        conn.execute("UPDATE employees SET leave_balance_hours = leave_balance_hours - ? WHERE id=?",
                     (req["leave_hours"], emp["id"]))
        _log_change(conn, req["id"], "balance_deducted", None, emp["id"],
                    {"balance": emp["leave_balance_hours"]}, {"balance": emp["leave_balance_hours"] - req["leave_hours"]})
    _apply_leave_to_schedule(conn, req)
    for n in needs:
        offer = _one(conn, "SELECT * FROM coverage_offers WHERE need_id=? AND status='accepted'", (n["id"],))
        if offer:
            _write_replacement_shift(conn, req, n, offer)
    result = _verify(conn, req["id"])
    conn.execute("UPDATE leave_requests SET verified=?, verification_json=? WHERE id=?",
                 (1 if result["covered"] else 0, json.dumps(result), req["id"]))
    notify(conn, emp["id"], req["id"], "approved", f"Your leave {period_text(req)} was approved and your schedule updated.")
    return result


def manager_decide(conn, request_id, approve, comment="", accept_uncovered_gap=False, actor="manager_sim"):
    req = get_request(conn, request_id)
    if req["leave_type"] == "sick":
        raise ServiceError("sick absences are Reported, not approved - official eligibility decisions are out of scope")
    if req["status"] not in ("awaiting_manager", "escalated"):
        raise ServiceError(f"request is '{req['status']}' - manager decision not allowed yet "
                           "(coverage must be secured or the request escalated first)")
    if not approve:
        with tx(conn):
            conn.execute("UPDATE leave_requests SET status='rejected', manager_comment=?, decided_at=? WHERE id=?",
                         (comment, now(), request_id))
            notify(conn, req["employee_id"], request_id, "rejected", f"Your leave request was declined by your manager. {comment}")
            audit(conn, request_id, actor, "manager_rejected", f"Manager REJECTED (simulated). {comment}")
        return None
    needs = _all(conn, "SELECT * FROM coverage_needs WHERE request_id=?", (request_id,))
    if any(n["status"] != "covered" for n in needs) and not accept_uncovered_gap:
        raise ServiceError("coverage gap remains: manager must explicitly accept the uncovered gap")
    # re-validate accepted replacements against the *current* schedule
    stale = []
    for n in needs:
        o = _one(conn, "SELECT * FROM coverage_offers WHERE need_id=? AND status='accepted'", (n["id"],))
        if o and rules.check_candidate(conn, n, o["candidate_id"], ignore_offer_id=o["id"]):
            stale.append((n, o))
    if stale:
        with tx(conn):
            for n, o in stale:
                conn.execute("UPDATE coverage_offers SET status='expired' WHERE id=?", (o["id"],))
                conn.execute("UPDATE coverage_needs SET status='open' WHERE id=?", (n["id"],))
            conn.execute("UPDATE leave_requests SET status='seeking_coverage' WHERE id=?", (request_id,))
            audit(conn, request_id, "system", "approval_blocked",
                  "Accepted replacement no longer valid at approval time; reopened coverage search")
        raise ServiceError("an accepted replacement is no longer eligible; coverage search reopened")
    with tx(conn):
        conn.execute("UPDATE leave_requests SET manager_comment=? WHERE id=?", (comment, request_id))
        audit(conn, request_id, actor, "manager_approved", f"Manager APPROVED (simulated). {comment}")
        result = _apply_approved(conn, req, needs)
        audit(conn, request_id, "system", "schedule_updated",
              "Leave record, balance and shift schedule updated atomically; coverage "
              + ("verified" if result["covered"] else "NOT fully verified"), result)
    return result


# ---------- 6. verification ----------
def _verify(conn, request_id):
    gaps = []
    for s in _all(conn, "SELECT * FROM shifts WHERE leave_request_id=? AND status='on_leave'", (request_id,)):
        need = rules.min_staff(conn, s["role"])
        for h in range(s["start_hour"], s["end_hour"]):
            have = rules.staffing_count(conn, s["role"], s["date"], h)
            if have < need:
                gaps.append({"date": s["date"], "hour": h, "role": s["role"], "have": have, "need": need})
    return {"covered": not gaps, "gaps": gaps}


def verify_coverage(conn, request_id):
    req = get_request(conn, request_id)
    if req["status"] not in ("approved", "reported"):
        raise ServiceError("coverage can only be verified after approval (or for a reported sick absence)")
    res = _verify(conn, request_id)
    with tx(conn):
        conn.execute("UPDATE leave_requests SET verified=?, verification_json=? WHERE id=?",
                     (1 if res["covered"] else 0, json.dumps(res), request_id))
    return res


# ---------- views ----------
def request_context(conn, request_id):
    req = get_request(conn, request_id)
    emp = _one(conn, "SELECT id,name,role,qualifications,leave_balance_hours,max_weekly_hours FROM employees WHERE id=?", (req["employee_id"],))
    return {"request": req, "employee": emp,
            "shifts_in_leave_period": _all(conn, "SELECT id,date,start_hour,end_hour,role,status FROM shifts "
                                              "WHERE employee_id=? AND date BETWEEN ? AND ? ORDER BY date,start_hour",
                                              (emp["id"], req["date"], req.get("end_date") or req["date"])),
            "coverage_needs": describe_needs(conn, request_id)}


def get_policies(conn):
    return {r["key"]: r["text"] for r in conn.execute("SELECT key,text FROM policies")}
