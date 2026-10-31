import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shiftrescue import db, seed  # noqa: E402

BASE = date(2026, 11, 2)    # a Monday
TODAY = date(2026, 10, 30)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_db(c)
    seed.seed(c, BASE)
    yield c
    c.close()


def emp(conn, name):
    return conn.execute("SELECT id FROM employees WHERE name=?", (name,)).fetchone()["id"]


def day(n):
    from datetime import timedelta
    return (BASE + timedelta(days=n)).isoformat()


@pytest.fixture(autouse=True)
def _no_real_api_calls(monkeypatch):
    """Safety net: automated tests must never reach the real Claude API."""
    from shiftrescue import agent

    def blocked(*a, **k):
        raise RuntimeError("Real API calls are disabled in tests - pass a stub client")
    monkeypatch.setattr(agent, "make_client", blocked)
