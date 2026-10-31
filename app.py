"""Nasaq - Intelligent Leave & Workforce Coordination. Run: streamlit run app.py"""
import json
import os
from datetime import date, timedelta
from html import escape

import pandas as pd
import streamlit as st

from shiftrescue import agent, db, rules, scenarios, seed, services as sv, ui

st.set_page_config(page_title="Nasaq", page_icon=":material/calendar_month:", layout="wide")

# ------------------------------------------------------------------ style
CSS = """
<style>
html, body, [class*="css"], .stApp { font-family: 'Segoe UI', Inter, -apple-system, Roboto, Helvetica, Arial, sans-serif; }
.stApp { background: #F6F8FB; }
footer { visibility: hidden; }
.block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1280px; }
h1, h2, h3, h4 { color: #0B1F3A; letter-spacing: -0.01em; }
[data-testid="stSidebar"] { background: #0B1F3A; }
[data-testid="stSidebar"] * { color: #D5E0F0; }
[data-testid="stSidebar"] hr { border-color: #1D3557; }
.nq-logo { font-size: 1.9rem; font-weight: 700; color: #FFFFFF !important; letter-spacing: -0.03em; display:flex; align-items:center; gap:.5rem; }
.nq-logo span { color: #FFFFFF !important; }
.nq-logo .dot { color: #2BB7C4 !important; }
.nq-tag { font-size: .78rem; color: #8FA7C6 !important; margin: .1rem 0 1.2rem 0; line-height:1.35; }
.nq-title { font-size: 1.65rem; font-weight: 650; color: #0B1F3A; margin: 0; }
.nq-sub { color: #5B6B82; margin: .15rem 0 1.4rem 0; font-size: .95rem; }
.nq-card { background:#fff; border:1px solid #E2E8F0; border-radius:12px; padding:1rem 1.15rem; margin-bottom:.8rem; color:#0B1F3A; }
.nq-card.attn { border-left:4px solid #D9534F; }
.nq-card.warn { border-left:4px solid #E0A100; }
.nq-card.info { border-left:4px solid #0E8F9B; }
.nq-kpi { background:#fff; border:1px solid #E2E8F0; border-radius:12px; padding:.9rem 1.1rem; }
.nq-kpi .v { font-size:1.9rem; font-weight:650; color:#0B1F3A; line-height:1.1; }
.nq-kpi .l { font-size:.78rem; color:#5B6B82; text-transform:uppercase; letter-spacing:.05em; margin-top:.2rem; }
.nq-h { font-size:1.02rem; font-weight:650; color:#0B1F3A; margin:1.4rem 0 .6rem 0; }
.nq-muted { color:#5B6B82; font-size:.88rem; }
.pill { display:inline-block; padding:.12rem .6rem; border-radius:999px; font-size:.76rem; font-weight:600; border:1px solid transparent; white-space:nowrap; }
.pill.info { background:#E6F1FB; color:#134A86; border-color:#CFE2F6; }
.pill.warn { background:#FFF4D6; color:#7A5300; border-color:#F3E1A8; }
.pill.danger { background:#FDE8E8; color:#9B1C1C; border-color:#F6CACA; }
.pill.success { background:#DDF4EE; color:#0B6B55; border-color:#BCE8DD; }
.pill.sick { background:#F3E8F7; color:#6B2B86; border-color:#E4CFEE; }
.pill.neutral { background:#EDF0F4; color:#42526B; border-color:#DCE2EA; }
.tblwrap { overflow-x:auto; }
table.nq { border-collapse:separate; border-spacing:0; width:100%; background:#fff; border:1px solid #E2E8F0; border-radius:10px; overflow:hidden; font-size:.84rem; }
table.nq th { background:#F0F4F9; color:#42526B; font-weight:600; text-align:left; padding:.5rem .6rem; border-bottom:1px solid #E2E8F0; white-space:nowrap; }
table.nq td { padding:.4rem .6rem; border-bottom:1px solid #EEF2F6; vertical-align:top; color:#0B1F3A; }
table.nq tr:last-child td { border-bottom:none; }
.chip { display:inline-block; padding:.08rem .45rem; margin:1px 2px 1px 0; border-radius:6px; font-size:.76rem; font-weight:600; border:1px solid transparent; white-space:nowrap; }
.chip.shift { background:#E8F0FA; color:#1F4E8C; border-color:#D3E2F4; }
.chip.leave { background:#FEF3C7; color:#92400E; border-color:#F6DFA0; }
.chip.sick { background:#F3E8F7; color:#6B2B86; border-color:#E4CFEE; }
.chip.cover { background:#D5F2F0; color:#0B6B6F; border-color:#AEE3DF; }
.chip.pending { background:#FFFFFF; color:#7A5300; border:1px dashed #E0A100; }
.chip.gap { background:#FDE8E8; color:#9B1C1C; border-color:#F2B8B8; }
.chip.ok { background:#EAF7F1; color:#0B6B55; border-color:#CDEBDD; }
.legend { display:flex; flex-wrap:wrap; gap:.4rem; margin:.2rem 0 .8rem 0; }
.tl { position:relative; margin-left:.5rem; padding-left:1.4rem; border-left:2px solid #D5DEEA; }
.tl-item { position:relative; padding:0 0 1.05rem 0; }
.tl-item:before { content:''; position:absolute; left:-1.95rem; top:.25rem; width:.7rem; height:.7rem; border-radius:50%; background:#fff; border:2px solid #0E8F9B; }
.tl-item.success:before { background:#0E8F9B; }
.tl-item.warn:before { border-color:#E0A100; }
.tl-item.danger:before { border-color:#D9534F; background:#D9534F; }
.tl-item.sick:before { border-color:#8E44AD; }
.tl-item.neutral:before { border-color:#8A97AB; }
.tl-title { font-weight:600; color:#0B1F3A; }
.tl-meta { font-size:.76rem; color:#7A889D; margin-left:.4rem; font-weight:400; }
.tl-detail { color:#42526B; font-size:.88rem; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

LOGO = ('<div class="nq-logo"><svg width="26" height="26" viewBox="0 0 22 22" fill="none" aria-hidden="true">'
        '<path d="M3.5 16.5 L11 5.5 L18.5 12" stroke="#2BB7C4" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="3.5" cy="16.5" r="2.1" fill="#2BB7C4"/><circle cx="11" cy="5.5" r="2.1" fill="#FFFFFF"/>'
        '<circle cx="18.5" cy="12" r="2.1" fill="#2BB7C4"/></svg><span>nasaq<span class="dot">.</span></span></div>')


# ------------------------------------------------------------------ bootstrap & helpers
def _bootstrap():
    c = db.connect()
    db.init_db(c)          # additive/idempotent; never resets data
    if c.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        seed.seed(c, seed.next_monday())
    c.close()


_bootstrap()
conn = db.connect()
HAS_KEY = bool(os.getenv("ANTHROPIC_API_KEY"))


def rows(sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def ename(eid):
    r = conn.execute("SELECT name FROM employees WHERE id=?", (eid,)).fetchone()
    return r["name"] if r else "Unknown"


def pill(text, tone="neutral"):
    return f'<span class="pill {tone}">{escape(text)}</span>'


def status_pill(r):
    label, tone = ui.status_label(r)
    return pill(label, tone)


def page_header(title, subtitle):
    st.markdown(f'<div class="nq-title">{escape(title)}</div><div class="nq-sub">{escape(subtitle)}</div>', unsafe_allow_html=True)


def section(title):
    st.markdown(f'<div class="nq-h">{escape(title)}</div>', unsafe_allow_html=True)


def run_agent_ui(rid, event):
    if not HAS_KEY:
        sv.audit(conn, rid, "system", "agent_error", "ANTHROPIC_API_KEY is not set - agent cannot run. See README.")
        st.session_state["flash_warn"] = ("The request was saved, but Nasaq's AI agent is not configured (no API key found). "
                                          "Add your key to .env, restart the app, then run the agent from "
                                          "Demo Scenarios > Admin / Troubleshooting.")
        return
    with st.spinner(f"Nasaq is working on request #{rid}..."):
        agent.safe_run_agent(conn, rid, event)


def guarded(fn, *a, **k):
    """Run a service call; show safeguard refusals as plain-language messages."""
    try:
        return fn(*a, **k)
    except sv.ServiceError as e:
        st.error(f"This can't be done: {e}")


def flash(msg):
    st.session_state["flash"] = msg


def show_flash():
    msg = st.session_state.pop("flash", None)
    warn = st.session_state.pop("flash_warn", None)
    if msg:
        st.success(msg)
    if warn:
        st.warning(warn)


def base_monday() -> date:
    r = conn.execute("SELECT value FROM meta WHERE key='base_monday'").fetchone()
    return date.fromisoformat(r[0]) if r else date.today()


def cov_text(r):
    cs = sv.coverage_summary(conn, r["id"])
    return {"not_analyzed": "Assessing", "no_gap": "No cover needed", "covered": f"{cs['covered']}/{cs['total']} covered",
            "seeking": f"{cs['covered']}/{cs['total']} covered",
            "escalated": f"{cs['covered']}/{cs['total']} covered, shortage"}[cs["state"]]


def need_lines(request_id):
    for n in sv.describe_needs(conn, request_id):
        acc = [o for o in n["offers"] if o["status"] == "accepted"]
        pend = [o for o in n["offers"] if o["status"] == "pending"]
        if acc:
            who = f'{pill("Covered", "success")} by <b>{escape(acc[0]["candidate_name"])}</b>'
        elif pend:
            who = f'{pill("Offer pending", "info")} to <b>{escape(pend[0]["candidate_name"])}</b>'
        elif n["status"] == "uncovered":
            who = pill("Uncovered", "danger")
        else:
            who = pill("Open", "warn")
        declined = sum(o["status"] == "declined" for o in n["offers"])
        extra = f' <span class="nq-muted">({declined} declined)</span>' if declined else ""
        st.markdown(f'<div style="margin:.15rem 0">{escape(n["role"])} · {n["date"]} {n["start_hour"]:02d}:00-{n["end_hour"]:02d}:00 &nbsp; {who}{extra}</div>',
                    unsafe_allow_html=True)


def staffing_table(dates):
    head = "".join(f"<th>{date.fromisoformat(d).strftime('%a %d %b')}</th>" for d in dates)
    gaps_by_date = {d: ui.staffing_gaps(conn, d) for d in dates}
    body = ""
    for role in [r["role"] for r in rows("SELECT role FROM staffing_minimums ORDER BY role")]:
        cells = ""
        for d in dates:
            g = gaps_by_date[d].get(role, [])
            cells += ("<td>" + "".join(f'<span class="chip gap">Below minimum {a:02d}-{b:02d}</span>' for a, b in g) + "</td>") if g \
                else '<td><span class="chip ok">Covered</span></td>'
        body += f"<tr><td><b>{escape(role)}</b></td>{cells}</tr>"
    st.markdown(f'<div class="tblwrap"><table class="nq"><tr><th>Role</th>{head}</tr>{body}</table></div>', unsafe_allow_html=True)


# ------------------------------------------------------------------ sidebar
PAGES = ["Overview", "My Leave", "Manager Workspace", "Workforce Calendar", "AI Activity", "Demo Scenarios"]
with st.sidebar:
    st.markdown(LOGO, unsafe_allow_html=True)
    st.markdown('<div class="nq-tag">Intelligent Leave &amp; Workforce Coordination<br>Time off, operations on.</div>', unsafe_allow_html=True)
    page = st.radio("Navigation", PAGES, label_visibility="collapsed", key="nav")
    st.divider()
    st.caption("AI coordination: " + ("active" if HAS_KEY else "not configured"))


# ================================================================== pages
def page_overview():
    page_header("Overview", "Leave requests, coverage and items that need attention.")
    reqs = ui.visible_requests(conn)
    action = [r for r in reqs if r["status"] in ("awaiting_manager", "escalated")]
    ongoing = [r for r in reqs if r["status"] in ("submitted", "seeking_coverage")]
    sick = [r for r in reqs if r["status"] == "reported"]
    done = [r for r in reqs if r["status"] == "approved"]
    kpis = [("Open requests", len([r for r in reqs if r["status"] not in ("approved", "rejected", "reported")])),
            ("Manager action required", len(action)), ("Coverage in progress", len(ongoing)),
            ("Sick absences reported", len(sick)), ("Approved and verified", len([r for r in done if r["verified"] == 1]))]
    for col, (label, val) in zip(st.columns(5), kpis):
        col.markdown(f'<div class="nq-kpi"><div class="v">{val}</div><div class="l">{label}</div></div>', unsafe_allow_html=True)

    section("Needs attention")
    items = []
    for r in action:
        what = "Escalated: some shifts have no available replacement" if r["status"] == "escalated" else "Coverage secured, awaiting manager approval"
        items.append(("attn" if r["status"] == "escalated" else "warn", f"#{r['id']} {r['employee']} · {ui.short_period(r)}", what))
    for r in sick:
        if sv.coverage_summary(conn, r["id"])["state"] == "escalated":
            items.append(("attn", f"#{r['id']} {r['employee']} · sick {ui.short_period(r)}", "Staffing shortage escalated (the absence stays recorded)"))
    for r in done:
        if r["verified"] == 0:
            items.append(("attn", f"#{r['id']} {r['employee']} · {ui.short_period(r)}", "Approved with remaining coverage gaps"))
    pend = rows("SELECT COUNT(*) AS n FROM coverage_offers WHERE status='pending'")[0]["n"]
    if pend:
        items.append(("info", f"{pend} coverage offer(s) waiting for a response", "Replacement candidates have been notified"))
    if not items:
        st.markdown('<div class="nq-card info">All clear. Nothing needs attention right now.</div>', unsafe_allow_html=True)
    for kind, title, detail in items:
        st.markdown(f'<div class="nq-card {kind}"><b>{escape(title)}</b><br><span class="nq-muted">{escape(detail)}</span></div>', unsafe_allow_html=True)

    section("Staffing coverage - next 7 days")
    b = base_monday()
    start = b if date.today() <= b + timedelta(days=13) else date.today()
    staffing_table(ui.date_span(start, 7))

    section("Recent requests")
    if not reqs:
        st.caption("No requests yet. Submit one from My Leave, or load a scenario in Demo Scenarios.")
    else:
        st.dataframe(pd.DataFrame([{
            "ID": r["id"], "Employee": r["employee"], "Type": ui.leave_kind(r), "Period": ui.short_period(r),
            "Status": ui.status_label(r)[0], "Coverage": cov_text(r), "Next step": ui.next_step(conn, r)} for r in reqs]),
            hide_index=True, width="stretch")


def page_my_leave():
    page_header("My Leave", "Request time off, report a sick absence and track your requests.")
    show_flash()
    emps = rows("SELECT * FROM employees WHERE is_manager=0 ORDER BY name")
    sel = st.selectbox("Viewing as", emps, format_func=lambda e: f"{e['name']} · {e['role']}")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown(f'<div class="nq-kpi"><div class="v">{sel["leave_balance_hours"]:g} h</div>'
                    f'<div class="l">Annual leave balance</div></div>', unsafe_allow_html=True)
        section("New request")
        kind = st.radio("What would you like to do?", ["Annual leave", "Report sick absence"], horizontal=True)
        sick = kind == "Report sick absence"
        today = date.today()
        min_start = today if sick else today + timedelta(days=1)
        d1 = st.date_input("From", value=max(base_monday(), min_start), min_value=min_start, key="lv_from")
        d2 = st.date_input("To (up to 7 days)", value=d1, min_value=d1, max_value=d1 + timedelta(days=6), key="lv_to")
        hourly = False
        h1, h2 = 0, 24
        if d1 == d2:
            hourly = st.checkbox("Only part of the day (hourly)", value=False)
            if hourly:
                h1, h2 = st.slider("Hours away", 0, 24, (12, 16))
        else:
            st.caption("Multi-day requests cover full days.")
        ls, le = (h1, h2) if hourly else (0, 24)
        hrs = sum(max(0, min(s["end_hour"], le) - max(s["start_hour"], ls))
                  for i in range((d2 - d1).days + 1) for s in rules.shifts_on(conn, sel["id"], (d1 + timedelta(days=i)).isoformat()))
        reason = st.text_input("Reason (optional)")
        if hrs:
            msg = f"You are scheduled for **{hrs:g} h** in this period."
            if not sick:
                msg += f" Balance after approval: **{sel['leave_balance_hours'] - hrs:g} h**."
            st.info(msg)
        else:
            st.warning("You have no scheduled shifts in this period.")
        if sick:
            st.caption("A sick absence is recorded immediately as Reported and does not use your annual balance. "
                       "Nasaq does not verify medical documents or decide sick-leave eligibility.")
        else:
            st.caption("Annual leave needs coverage (if staffing would drop below the minimum) and your manager's approval.")
        if st.button("Report sick absence" if sick else "Submit leave request", type="primary"):
            rid = guarded(sv.create_leave_request, conn, sel["id"], d1.isoformat(), h1 if hourly else None,
                          h2 if hourly else None, "sick" if sick else "annual", reason, end_date=d2.isoformat())
            if rid:
                st.session_state["selected_req"] = rid
                run_agent_ui(rid, "sick_reported" if sick else "new_request")
                flash(f"Request #{rid} {'recorded as Reported' if sick else 'submitted'}. Follow progress under AI Activity.")
                st.rerun()
    with right:
        section("Upcoming shifts")
        up = rows("SELECT date,start_hour,end_hour,status,source FROM shifts WHERE employee_id=? AND date>=? ORDER BY date,start_hour LIMIT 8",
                  (sel["id"], date.today().isoformat()))
        if up:
            st.dataframe(pd.DataFrame([{"Date": u["date"], "Hours": f"{u['start_hour']:02d}-{u['end_hour']:02d}",
                                        "Status": "On leave" if u["status"] == "on_leave" else ("Cover" if u["source"] == "coverage" else "Shift")}
                                       for u in up]), hide_index=True, width="stretch")
        else:
            st.caption("No upcoming shifts.")

    section("My requests")
    mine = [r for r in ui.visible_requests(conn) if r["employee_id"] == sel["id"]]
    if not mine:
        st.caption("You have not made any requests yet.")
    for r in mine:
        extra = f' · {escape(r["reason"])}' if r.get("reason") else ""
        st.markdown(f'<div class="nq-card"><b>{ui.leave_kind(r)}</b> · {escape(ui.short_period(r))} · {r["leave_hours"]:g} h '
                    f'&nbsp;{status_pill(r)}<br><span class="nq-muted">{escape(ui.next_step(conn, r))}{extra}</span></div>', unsafe_allow_html=True)
    section("Notifications")
    notes = rows("SELECT * FROM notifications WHERE recipient_id=? ORDER BY id DESC LIMIT 8", (sel["id"],))
    if not notes:
        st.caption("No notifications.")
    for n in notes:
        st.markdown(f'<div class="nq-card"><span class="nq-muted">{ui.fmt_ts(n["created_at"])} · {ui.NOTIFICATION_LABEL.get(n["kind"], n["kind"])}</span>'
                    f'<br>{escape(n["message"])}</div>', unsafe_allow_html=True)


def page_manager():
    page_header("Manager Workspace", "Approvals and escalations that need you, plus coverage in progress.")
    mgr = rows("SELECT * FROM employees WHERE is_manager=1")[0]
    reqs = ui.visible_requests(conn)
    action = [r for r in reqs if r["status"] in ("awaiting_manager", "escalated")]
    ongoing = [r for r in reqs if r["status"] in ("submitted", "seeking_coverage")]
    sick = [r for r in reqs if r["status"] == "reported"]
    show_flash()
    st.caption(f"Signed in as {mgr['name']}. Approve and Decline are simulated manager responses for the demo.")

    section(f"Action required ({len(action)})")
    shortages = [r for r in sick if sv.coverage_summary(conn, r["id"])["state"] == "escalated"]
    if shortages:
        st.warning(f"{len(shortages)} sick-absence staffing shortage(s) escalated. See Reported sick absences below.")
    if not action:
        st.markdown('<div class="nq-card info">No approvals or escalations are waiting for you.</div>', unsafe_allow_html=True)
    for r in action:
        with st.container(border=True):
            st.markdown(f'**#{r["id"]} {escape(r["employee"])}** · {ui.leave_kind(r)} · {escape(ui.short_period(r))} · {r["leave_hours"]:g} h &nbsp;{status_pill(r)}',
                        unsafe_allow_html=True)
            if r["status"] == "escalated":
                st.error(f"Escalated by Nasaq (the leave has not been denied): {r['escalation_reason']}")
            need_lines(r["id"])
            gap_ok = True
            if r["status"] == "escalated":
                gap_ok = st.checkbox("I accept the uncovered staffing gap(s)", key=f"gap{r['id']}")
            comment = st.text_input("Comment (optional)", key=f"cm{r['id']}")
            c1, c2, _ = st.columns([1, 1, 3])
            if c1.button("Approve", key=f"ap{r['id']}", type="primary"):
                if guarded(sv.manager_decide, conn, r["id"], True, comment, accept_uncovered_gap=gap_ok):
                    run_agent_ui(r["id"], "approved_and_applied")
                    flash(f"Request #{r['id']} approved. Schedule updated.")
                    st.rerun()
            if c2.button("Decline", key=f"rj{r['id']}"):
                if guarded(sv.manager_decide, conn, r["id"], False, comment) is None:
                    st.rerun()

    section(f"Ongoing coverage ({len(ongoing)})")
    if not ongoing:
        st.caption("No requests are currently looking for replacements.")
    for r in ongoing:
        with st.container(border=True):
            st.markdown(f'**#{r["id"]} {escape(r["employee"])}** · {escape(ui.short_period(r))} &nbsp;{status_pill(r)}', unsafe_allow_html=True)
            st.caption(ui.next_step(conn, r))
            need_lines(r["id"])

    section(f"Reported sick absences ({len(sick)})")
    st.caption("Recorded automatically. These do not need approval; Nasaq only arranges cover.")
    labels = {"not_analyzed": ("Assessing impact", "info"), "no_gap": ("Staffing still met", "success"),
              "covered": ("All shifts covered", "success"), "seeking": ("Finding cover", "info"),
              "escalated": ("Staffing shortage", "danger")}
    if not sick:
        st.caption("No sick absences reported.")
    for r in sick:
        txt, tone = labels[sv.coverage_summary(conn, r["id"])["state"]]
        with st.container(border=True):
            st.markdown(f'**#{r["id"]} {escape(r["employee"])}** · {escape(ui.short_period(r))} &nbsp;{pill("Reported", "sick")} {pill(txt, tone)}',
                        unsafe_allow_html=True)
            if r["escalation_reason"]:
                st.warning(f"Nasaq: {r['escalation_reason']}")
            need_lines(r["id"])

    with st.expander("Activity history", expanded=False):
        done = [r for r in reqs if r["status"] in ("approved", "rejected")]
        if done:
            st.markdown("**Completed requests**")
            st.dataframe(pd.DataFrame([{"ID": r["id"], "Employee": r["employee"], "Period": ui.short_period(r),
                                        "Outcome": ui.status_label(r)[0], "Decided": ui.fmt_ts(r["decided_at"] or "")} for r in done]),
                         hide_index=True, width="stretch")
        st.markdown("**Past notifications**")
        hist = rows("SELECT created_at, kind, message, request_id FROM notifications WHERE recipient_id=? ORDER BY id DESC LIMIT 60", (mgr["id"],))
        st.dataframe(pd.DataFrame([{"When": ui.fmt_ts(h["created_at"]), "Type": ui.NOTIFICATION_LABEL.get(h["kind"], h["kind"]),
                                    "Request": h["request_id"], "Message": h["message"]} for h in hist]),
                     hide_index=True, width="stretch")


def page_calendar():
    page_header("Workforce Calendar", "Shifts, absences and replacement coverage across your team.")
    reqs = ui.visible_requests(conn)
    c1, c2, c3, c4 = st.columns([2, 1, 1.4, 2])
    jump = c4.selectbox("Jump to request", [None] + reqs, format_func=lambda r: "None" if r is None else ui.request_label(r))
    if jump:
        start = date.fromisoformat(jump["date"])
        days = min(14, (date.fromisoformat(jump["end_date"] or jump["date"]) - start).days + 1)
        c1.caption(f"Showing the dates of request #{jump['id']}.")
    else:
        start = c1.date_input("Start date", value=base_monday(), key="cal_start")
        days = c2.selectbox("Days", [1, 3, 7, 14], index=2)
    role = c3.selectbox("Role", ["All roles"] + [r["role"] for r in rows("SELECT role FROM staffing_minimums ORDER BY role")])
    dates = ui.date_span(start, days)

    tab_cal, tab_tbl = st.tabs(["Calendar", "Detailed table"])
    with tab_cal:
        st.markdown('<div class="legend"><span class="chip shift">Shift</span><span class="chip leave">Annual leave</span>'
                    '<span class="chip sick">Sick absence</span><span class="chip cover">Replacement cover</span>'
                    '<span class="chip pending">Leave requested</span><span class="chip gap">Below minimum staffing</span></div>',
                    unsafe_allow_html=True)
        section("Staffing check")
        staffing_table(dates)
        section("Schedule")
        emps, cells = ui.calendar_cells(conn, dates, None if role == "All roles" else role)
        head = "".join(f"<th>{date.fromisoformat(d).strftime('%a %d %b')}</th>" for d in dates)
        body = ""
        for e in emps:
            tds = "".join("<td>" + "".join(f'<span class="chip {c}">{escape(l)}</span>' for c, l in cells.get((e["id"], d), [])) + "</td>"
                          for d in dates)
            body += f'<tr><td><b>{escape(e["name"])}</b><br><span class="nq-muted">{escape(e["role"])}</span></td>{tds}</tr>'
        st.markdown(f'<div class="tblwrap"><table class="nq"><tr><th>Employee</th>{head}</tr>{body}</table></div>', unsafe_allow_html=True)
    with tab_tbl:
        ph = ",".join("?" * len(dates))
        q = ("SELECT s.date AS Date, e.name AS Employee, s.role AS Role, s.start_hour AS Start, s.end_hour AS End, "
             "CASE WHEN s.status='on_leave' THEN CASE WHEN r.leave_type='sick' THEN 'Sick absence' ELSE 'Annual leave' END "
             "WHEN s.source='coverage' THEN 'Replacement cover' ELSE 'Shift' END AS Type, s.leave_request_id AS Request "
             "FROM shifts s JOIN employees e ON e.id=s.employee_id LEFT JOIN leave_requests r ON r.id=s.leave_request_id "
             f"WHERE s.date IN ({ph}) " + ("" if role == "All roles" else "AND s.role=? ") + "ORDER BY s.date, s.role, s.start_hour")
        st.dataframe(pd.DataFrame(rows(q, dates + ([] if role == "All roles" else [role]))), hide_index=True, width="stretch")


def page_activity():
    page_header("AI Activity", "What Nasaq did on each request, step by step.")
    reqs = ui.visible_requests(conn)
    if not reqs:
        st.info("No requests yet. Submit one from My Leave or load a scenario in Demo Scenarios.")
        return
    ids = [r["id"] for r in reqs]
    idx = ids.index(st.session_state["selected_req"]) if st.session_state.get("selected_req") in ids else 0
    r = st.selectbox("Request", reqs, index=idx, format_func=ui.request_label)
    st.markdown(f'**{escape(r["employee"])}** · {ui.leave_kind(r)} · {escape(ui.short_period(r))} &nbsp;{status_pill(r)}', unsafe_allow_html=True)
    st.caption(ui.next_step(conn, r))
    if r["verification_json"]:
        v = json.loads(r["verification_json"])
        if v["covered"]:
            st.success("Coverage verified: minimum staffing is met for every affected hour.")
        else:
            st.error("Coverage gaps remain: " + "; ".join(f"{g['date']} {g['hour']:02d}:00 ({g['role']})" for g in v["gaps"][:8]))

    section("Timeline")
    steps = ui.timeline(conn, r["id"])
    if not steps:
        st.caption("No activity yet.")
    html = '<div class="tl">'
    for s_ in steps:
        who = {"agent": "Nasaq AI", "system": "System", "employee_sim": "Employee (simulated)",
               "manager_sim": "Manager (simulated)"}.get(s_["actor"], s_["actor"])
        html += (f'<div class="tl-item {s_["tone"]}"><div class="tl-title">{escape(s_["title"])}'
                 f'<span class="tl-meta">{ui.fmt_ts(s_["ts"])} · {escape(who)}</span></div>'
                 f'<div class="tl-detail">{escape(s_["detail"])}</div></div>')
    st.markdown(html + "</div>", unsafe_allow_html=True)

    with st.expander("Technical details: full audit trail and tool calls", expanded=False):
        st.dataframe(pd.DataFrame(ui.raw_events(conn, r["id"])), hide_index=True, width="stretch")
        st.markdown("**Coverage offers**")
        st.dataframe(pd.DataFrame(rows(
            "SELECT o.id,e.name AS candidate,o.status,o.rationale,o.created_at,o.responded_at,o.responded_by "
            "FROM coverage_offers o JOIN employees e ON e.id=o.candidate_id WHERE o.request_id=?", (r["id"],))),
            hide_index=True, width="stretch")
        st.markdown("**Schedule changes**")
        st.dataframe(pd.DataFrame(rows("SELECT change_type,shift_id,employee_id,before_json,after_json,created_at "
                                       "FROM schedule_changes WHERE request_id=?", (r["id"],))), hide_index=True, width="stretch")


def page_demo():
    page_header("Demo Scenarios", "Load a ready-made situation, then respond as the employee and manager.")
    st.info("Demo environment: scenarios submit real requests as the named employee. Employee responses on this page and "
            "manager responses in the Manager Workspace are simulated for demonstration.")
    show_flash()
    items = list(scenarios.SCENARIOS.items())
    for i in range(0, len(items), 3):
        for col, (key, s) in zip(st.columns(3), items[i:i + 3]):
            with col, st.container(border=True):
                tags = [pill("Sick absence" if s["leave_type"] == "sick" else "Annual leave", "sick" if s["leave_type"] == "sick" else "info")]
                if s.get("end_day", s["day"]) > s["day"]:
                    tags.append(pill("Multi-day", "neutral"))
                if s["start"] is not None:
                    tags.append(pill("Hourly", "neutral"))
                st.markdown(f"**{escape(s['title'])}**<br>{' '.join(tags)}", unsafe_allow_html=True)
                st.write(s["blurb"])
                st.caption(f"Employee: {s['employee']}")
                if st.button("Run scenario", key=f"sc_{key}", width="stretch"):
                    rid = guarded(scenarios.create_scenario_request, conn, key)
                    if rid:
                        st.session_state["selected_req"] = rid
                        run_agent_ui(rid, "sick_reported" if s["leave_type"] == "sick" else "new_request")
                        flash(f"Scenario started as request #{rid}. Follow it under AI Activity.")
                        st.rerun()

    section("Simulated employee responses")
    pending = rows("SELECT o.*, n.date, n.start_hour, n.end_hour, n.role FROM coverage_offers o "
                   "JOIN coverage_needs n ON n.id=o.need_id WHERE o.status='pending' ORDER BY o.id")
    if not pending:
        st.caption("No coverage offers are waiting for a response.")
    for o in pending:
        with st.container(border=True):
            c1, c2, c3 = st.columns([5, 1, 1])
            c1.markdown(f"**{escape(ename(o['candidate_id']))}** is asked to cover {escape(o['role'])} · {o['date']} "
                        f"{o['start_hour']:02d}:00-{o['end_hour']:02d}:00 (request #{o['request_id']})")
            if o["rationale"]:
                c1.caption(f"Why this person: {o['rationale']}")
            if c2.button("Accept", key=f"acc{o['id']}", type="primary"):
                if guarded(sv.respond_to_offer, conn, o["id"], True):
                    run_agent_ui(o["request_id"], "offer_accepted")
                    st.rerun()
            if c3.button("Decline", key=f"dec{o['id']}"):
                if guarded(sv.respond_to_offer, conn, o["id"], False, "Not available"):
                    run_agent_ui(o["request_id"], "offer_declined")
                    st.rerun()
    section("Simulated manager response")
    st.caption("Open the Manager Workspace to approve or decline requests.")

    with st.expander("Admin / Troubleshooting", expanded=False):
        st.caption(f"Model: {agent.MODEL} · API key: {'found' if HAS_KEY else 'missing'}")
        reqs = ui.visible_requests(conn)
        if reqs:
            pick = st.selectbox("Re-run the agent on a request", reqs, format_func=ui.request_label)
            if st.button("Re-run agent"):
                run_agent_ui(pick["id"], "manual_rerun")
                st.rerun()
        st.divider()
        st.markdown("**Reset demo data** (deletes all requests and restores the original sample data)")
        sure = st.checkbox("I understand this cannot be undone", key="reset_ok")
        if st.button("Reset demo data", disabled=not sure):
            db.reset_db(conn)
            seed.seed(conn, seed.next_monday())
            st.rerun()


PAGE_FUNCS = {"Overview": page_overview, "My Leave": page_my_leave, "Manager Workspace": page_manager,
              "Workforce Calendar": page_calendar, "AI Activity": page_activity, "Demo Scenarios": page_demo}
try:
    PAGE_FUNCS[page]()
except Exception as exc:  # keep the demo usable; details stay available to developers
    st.error("Something went wrong while loading this page. Please try again or choose another page.")
    with st.expander("Developer details"):
        st.exception(exc)
