"""Realistic synthetic data for a small clinic. Deterministic given a base Monday."""
from __future__ import annotations

from datetime import date, timedelta

from . import db

NURSES = ["Layla Al-Harbi", "Omar Al-Qahtani", "Nora Al-Dosari", "Khalid Al-Mutairi",
          "Reem Al-Ghamdi", "Faisal Al-Shehri", "Huda Al-Zahrani", "Yousef Al-Otaibi"]
LAB = ["Hessa Al-Rashid", "Tariq Al-Subaie", "Maha Al-Anazi", "Bader Al-Harthi"]
RECEPTION = ["Amal Al-Jaber", "Salman Al-Khaldi", "Jawaher Al-Naimi", "Nasser Al-Bishi"]
PHARMA = ["Rana Al-Tamimi", "Ziad Al-Sudairi", "Lina Al-Qurashi"]
MANAGER = "Dr. Abdullah Al-Fahad"

POLICIES = {
    "min_staffing": "Every clinic hour must keep at least the minimum staff per role "
                    "(Nurse 2, Lab Technician 2, Receptionist 1, Pharmacist 1). Leave that would drop below the minimum needs a qualified replacement.",
    "qualification": "A replacement must be qualified for the exact role of the shift being covered. No cross-role coverage.",
    "rest": "Employees need at least 10 hours of rest between shifts and may not work more than 12 hours in a day.",
    "weekly_cap": "Replacement shifts may not push an employee over their contracted maximum weekly hours. Overtime is not allowed.",
    "offers": "Send one coverage offer at a time per shift. At most 4 offers per coverage need; after that, escalate to the manager.",
    "approval": "Leave is only approved by the manager. The AI agent may never approve or deny leave. Schedule changes happen only after manager approval.",
    "no_auto_deny": "If no replacement can be found the request is escalated to the manager with a summary. It is never denied automatically.",
    "balance": "Annual leave requires enough leave-balance hours. Sick leave does not consume balance.",
    "notice": "Leave must be requested for a future date.",
}


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()))


def seed(conn, base_monday: date, days: int = 14) -> None:
    emp_ids: dict[str, int] = {}

    def add(name, role, quals, cap=40, bal=120.0, seniority=3.0, extra=0, mgr=0):
        cur = conn.execute(
            "INSERT INTO employees(name,role,qualifications,max_weekly_hours,leave_balance_hours,seniority_years,"
            "prefers_extra_shifts,is_manager) VALUES (?,?,?,?,?,?,?,?)",
            (name, role, quals, cap, bal, seniority, extra, mgr))
        emp_ids[name] = cur.lastrowid
        return cur.lastrowid

    nurses = []
    for i, n in enumerate(NURSES):
        nurses.append(add(n, "Nurse", "Nurse", cap=16 if i == 6 else 40, seniority=1 + i,
                          extra=1 if i in (2, 3) else 0, bal=120))
    lab = [add(n, "Lab Technician", "Lab Technician", seniority=2 + i, bal=4 if i == 3 else 120,
               extra=1 if i == 1 else 0) for i, n in enumerate(LAB)]
    rec = [add(n, "Receptionist", "Receptionist", seniority=1 + i) for i, n in enumerate(RECEPTION)]
    pharma = [add(n, "Pharmacist", "Pharmacist", seniority=4 + i) for i, n in enumerate(PHARMA)]
    add(MANAGER, "Manager", "Manager", seniority=12, mgr=1)

    for role, m in (("Nurse", 2), ("Lab Technician", 2), ("Receptionist", 1), ("Pharmacist", 1)):
        conn.execute("INSERT INTO staffing_minimums VALUES (?,?)", (role, m))
    for k, v in POLICIES.items():
        conn.execute("INSERT INTO policies VALUES (?,?)", (k, v))

    def shift(emp, d, s, e, role):
        conn.execute("INSERT INTO shifts(employee_id,date,start_hour,end_hour,role) VALUES (?,?,?,?,?)",
                     (emp, d.isoformat(), s, e, role))

    for day in range(days):
        d = base_monday + timedelta(days=day)
        for k in (2 * day, 2 * day + 1):                     # day shift 07-15
            shift(nurses[k % 8], d, 7, 15, "Nurse")
        for k in (2 * day + 4, 2 * day + 5):                 # evening shift 15-23
            shift(nurses[k % 8], d, 15, 23, "Nurse")
        for k in (day, day + 1):
            shift(lab[k % 4], d, 8, 16, "Lab Technician")
            shift(rec[k % 4], d, 8, 16, "Receptionist")
        shift(pharma[day % 3], d, 9, 18, "Pharmacist")

    # The two other pharmacists are already on approved leave on the first Monday
    for p in pharma[1:]:
        conn.execute(
            "INSERT INTO leave_requests(employee_id,date,end_date,start_hour,end_hour,leave_type,reason,leave_hours,status,created_at,decided_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (p, base_monday.isoformat(), base_monday.isoformat(), 0, 24, "annual", "Pre-approved conference", 0, "approved", db.now(), db.now()))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('base_monday', ?)", (base_monday.isoformat(),))
