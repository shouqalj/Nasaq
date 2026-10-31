"""Deterministic scheduling rules. The LLM can never bypass these: every service
call that changes state re-runs them in code."""
from __future__ import annotations

from datetime import date as _date, timedelta

MIN_REST_HOURS = 10
MAX_DAILY_HOURS = 12
MAX_OFFERS_PER_NEED = 4
ACTIVE_REQUEST_STATUSES = ("seeking_coverage", "awaiting_manager", "escalated", "reported")
COVERAGE_OPEN = ("seeking_coverage", "reported")   # statuses in which offers can be sent/answered


def _abs_hour(date_str: str, hour: int) -> int:
    return _date.fromisoformat(date_str).toordinal() * 24 + hour


def shifts_on(conn, employee_id: int, date: str, status: str = "scheduled"):
    return conn.execute(
        "SELECT * FROM shifts WHERE employee_id=? AND date=? AND status=? ORDER BY start_hour",
        (employee_id, date, status),
    ).fetchall()


def min_staff(conn, role: str) -> int:
    r = conn.execute("SELECT min_staff FROM staffing_minimums WHERE role=?", (role,)).fetchone()
    return r["min_staff"] if r else 0


def staffing_count(conn, role: str, date: str, hour: int, exclude_employee: int | None = None) -> int:
    q = ("SELECT COUNT(DISTINCT employee_id) FROM shifts WHERE role=? AND date=? AND status='scheduled' "
         "AND start_hour<=? AND end_hour>?")
    args: list = [role, date, hour, hour]
    if exclude_employee is not None:
        q += " AND employee_id<>?"
        args.append(exclude_employee)
    return conn.execute(q, args).fetchone()[0]


def coverage_gap_hours(conn, role: str, date: str, start: int, end: int, exclude_employee: int) -> list[int]:
    """Hours in [start,end) where staffing (without `exclude_employee`) is below the minimum."""
    need = min_staff(conn, role)
    return [h for h in range(start, end) if staffing_count(conn, role, date, h, exclude_employee) < need]


def reserved_windows(conn, candidate_id: int, ignore_offer_id: int | None = None):
    """Windows a candidate is already tentatively committed to (pending/accepted offers
    for requests that are still open)."""
    q = ("SELECT n.date, n.start_hour, n.end_hour FROM coverage_offers o "
         "JOIN coverage_needs n ON n.id=o.need_id JOIN leave_requests r ON r.id=o.request_id "
         "WHERE o.candidate_id=? AND o.status IN ('pending','accepted') "
         "AND NOT (r.leave_type='sick' AND o.status='accepted') "   # already written to shifts
         f"AND r.status IN ({','.join('?' * len(ACTIVE_REQUEST_STATUSES))})")
    args: list = [candidate_id, *ACTIVE_REQUEST_STATUSES]
    if ignore_offer_id is not None:
        q += " AND o.id<>?"
        args.append(ignore_offer_id)
    return conn.execute(q, args).fetchall()


def week_hours(conn, employee_id: int, date: str, ignore_offer_id: int | None = None) -> float:
    d = _date.fromisoformat(date)
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    total = conn.execute(
        "SELECT COALESCE(SUM(end_hour-start_hour),0) FROM shifts WHERE employee_id=? AND status='scheduled' "
        "AND date BETWEEN ? AND ?", (employee_id, monday.isoformat(), sunday.isoformat())).fetchone()[0]
    for w in reserved_windows(conn, employee_id, ignore_offer_id):
        if monday.isoformat() <= w["date"] <= sunday.isoformat():
            total += w["end_hour"] - w["start_hour"]
    return float(total)


def check_candidate(conn, need, candidate_id: int, ignore_offer_id: int | None = None) -> list[str]:
    """Return the list of violated rules. Empty list == eligible."""
    reasons: list[str] = []
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (candidate_id,)).fetchone()
    if not emp or not emp["active"]:
        return ["employee not found or inactive"]
    req = conn.execute("SELECT * FROM leave_requests WHERE id=?", (need["request_id"],)).fetchone()
    if candidate_id == req["employee_id"]:
        reasons.append("is the employee requesting leave")
    if emp["is_manager"]:
        reasons.append("managers are not part of the coverage pool")
    quals = {q.strip() for q in emp["qualifications"].split(",")}
    if need["role"] not in quals:
        reasons.append(f"not qualified for role '{need['role']}'")

    date, s, e = need["date"], need["start_hour"], need["end_hour"]
    ns, ne = _abs_hour(date, s), _abs_hour(date, e)

    # overlap with scheduled shifts / rest rule (look at +-1 day)
    d = _date.fromisoformat(date)
    for off in (-1, 0, 1):
        day = (d + timedelta(days=off)).isoformat()
        for sh in shifts_on(conn, candidate_id, day):
            ss, se = _abs_hour(day, sh["start_hour"]), _abs_hour(day, sh["end_hour"])
            if ss < ne and se > ns:
                reasons.append(f"already working {sh['date']} {sh['start_hour']}:00-{sh['end_hour']}:00")
            elif se <= ns and ns - se < MIN_REST_HOURS:
                reasons.append(f"rest rule: only {ns - se}h rest after shift ending {sh['date']} {sh['end_hour']}:00 (min {MIN_REST_HOURS}h)")
            elif ss >= ne and ss - ne < MIN_REST_HOURS:
                reasons.append(f"rest rule: only {ss - ne}h rest before shift starting {sh['date']} {sh['start_hour']}:00 (min {MIN_REST_HOURS}h)")

    # approved leave
    lv = conn.execute(
        "SELECT 1 FROM leave_requests WHERE employee_id=? AND date<=? AND COALESCE(end_date,date)>=? "
        "AND status IN ('approved','reported') AND start_hour<? AND end_hour>?", (candidate_id, date, date, e, s)).fetchone()
    if lv:
        reasons.append("on approved leave")

    # tentative commitments (pending/accepted offers): overlap AND rest rule apply to them too
    reserved = reserved_windows(conn, candidate_id, ignore_offer_id)
    for w in reserved:
        ws, we = _abs_hour(w["date"], w["start_hour"]), _abs_hour(w["date"], w["end_hour"])
        if ws < ne and we > ns:
            reasons.append("already holds another coverage offer for an overlapping window")
            break
        if we <= ns and ns - we < MIN_REST_HOURS:
            reasons.append(f"rest rule: only {ns - we}h after another pending/accepted coverage offer")
            break
        if ws >= ne and ws - ne < MIN_REST_HOURS:
            reasons.append(f"rest rule: only {ws - ne}h before another pending/accepted coverage offer")
            break

    hours = e - s
    day_hours = sum(sh["end_hour"] - sh["start_hour"] for sh in shifts_on(conn, candidate_id, date))
    day_hours += sum(w["end_hour"] - w["start_hour"] for w in reserved if w["date"] == date)
    if day_hours + hours > MAX_DAILY_HOURS:
        reasons.append(f"daily hours would exceed {MAX_DAILY_HOURS}h")
    wk = week_hours(conn, candidate_id, date, ignore_offer_id)
    if wk + hours > emp["max_weekly_hours"]:
        reasons.append(f"weekly hours cap: {wk:g}h + {hours}h > {emp['max_weekly_hours']:g}h")

    q = "SELECT status FROM coverage_offers WHERE need_id=? AND candidate_id=?"
    args: list = [need["id"], candidate_id]
    if ignore_offer_id is not None:
        q += " AND id<>?"
        args.append(ignore_offer_id)
    if conn.execute(q, args).fetchone():
        reasons.append("already offered this coverage need")
    return reasons
