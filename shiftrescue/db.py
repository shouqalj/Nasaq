"""SQLite persistence: schema, connection, transaction helper."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DEFAULT_DB = os.getenv("SHIFTRESCUE_DB", "data/shiftrescue.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
    qualifications TEXT NOT NULL,            -- comma separated
    max_weekly_hours REAL NOT NULL DEFAULT 40,
    leave_balance_hours REAL NOT NULL DEFAULT 0,
    seniority_years REAL NOT NULL DEFAULT 0,
    prefers_extra_shifts INTEGER NOT NULL DEFAULT 0,
    is_manager INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS staffing_minimums (role TEXT PRIMARY KEY, min_staff INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS policies (key TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    date TEXT NOT NULL, start_hour INTEGER NOT NULL, end_hour INTEGER NOT NULL,
    role TEXT NOT NULL, location TEXT NOT NULL DEFAULT 'Main Clinic',
    status TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | on_leave
    source TEXT NOT NULL DEFAULT 'base',        -- base | coverage | split
    covering_for INTEGER, leave_request_id INTEGER
);
CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    date TEXT NOT NULL, start_hour INTEGER NOT NULL, end_hour INTEGER NOT NULL,
    end_date TEXT,           -- last day of the leave (== date for single-day)
    leave_type TEXT NOT NULL, reason TEXT, leave_hours REAL NOT NULL,
    status TEXT NOT NULL,   -- annual: submitted|seeking_coverage|awaiting_manager|escalated|approved|rejected ; sick: reported
    manager_id INTEGER, escalation_reason TEXT, manager_comment TEXT,
    created_at TEXT NOT NULL, decided_at TEXT,
    verified INTEGER, verification_json TEXT
);
CREATE TABLE IF NOT EXISTS coverage_needs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES leave_requests(id),
    shift_id INTEGER NOT NULL REFERENCES shifts(id),
    role TEXT NOT NULL, date TEXT NOT NULL, start_hour INTEGER NOT NULL, end_hour INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'   -- open | offered | covered | uncovered
);
CREATE TABLE IF NOT EXISTS coverage_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    need_id INTEGER NOT NULL REFERENCES coverage_needs(id),
    request_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL REFERENCES employees(id),
    status TEXT NOT NULL,   -- pending | accepted | declined | withdrawn | expired
    rationale TEXT, created_at TEXT NOT NULL, responded_at TEXT,
    responded_by TEXT, response_note TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL, request_id INTEGER, kind TEXT NOT NULL,
    message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL, change_type TEXT NOT NULL, shift_id INTEGER,
    employee_id INTEGER, before_json TEXT, after_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, request_id INTEGER,
    actor TEXT NOT NULL,       -- agent | system | employee_sim | manager_sim
    event_type TEXT NOT NULL, message TEXT NOT NULL, payload TEXT
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions via tx()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before multi-day leave existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(leave_requests)")}
    if "end_date" not in cols:
        conn.execute("ALTER TABLE leave_requests ADD COLUMN end_date TEXT")
    conn.execute("UPDATE leave_requests SET end_date=date WHERE end_date IS NULL")


def reset_db(conn: sqlite3.Connection) -> None:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    init_db(conn)


@contextmanager
def tx(conn: sqlite3.Connection):
    """All-or-nothing write block."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
