"""Live labor checks: bounded windows, aggregates only, one query at a time.

Skipped unless ``SITEWATCH_LIVE=1``. Never prints or asserts on names.

    SITEWATCH_LIVE=1 python -m pytest tests/live/test_labor_live.py -v
"""

from collections import defaultdict
from datetime import date, timedelta

import pytest

from sitewatch_data_client._filters import business_today
from sitewatch_data_client.types._labor import TimecardFlag

WEEK = (date(2026, 10, 3), date(2026, 10, 9))  # one Sat-Fri pay week
PAY_PERIOD = (date(2026, 9, 26), date(2026, 10, 9))  # validated export window


@pytest.fixture(scope="module")
def labor(live_client):
    return live_client.labor


def test_timecards_reproduce_export(labor):
    """export_timecards.py 2026-09-26..10-09: 583 intervals, 2814.83 h, 111 EDITED."""
    report = labor.timecard_totals(*PAY_PERIOD)
    ivs = report.intervals
    assert len(ivs) == 583
    assert round(sum(i.hours for i in ivs), 2) == 2814.83
    assert sum(TimecardFlag.EDITED in i.flags for i in ivs) == 111
    assert report.whole_pay_weeks is True
    # Each total is rounded to 2 dp, so allow at most half a cent-hour per total.
    assert sum(t.total_hours for t in report.totals) == pytest.approx(
        sum(i.hours for i in ivs), abs=0.005 * len(report.totals)
    )
    assert sum(t.intervals for t in report.totals) == 583
    # Every OPEN fragment is part of an OPEN_CHAIN, and open_hours covers whole shifts.
    assert all(TimecardFlag.OPEN_CHAIN in i.flags for i in ivs if TimecardFlag.OPEN in i.flags)
    chain_hours = sum(i.hours for i in ivs if TimecardFlag.OPEN_CHAIN in i.flags)
    assert sum(t.open_hours for t in report.totals) == pytest.approx(
        chain_hours, abs=0.005 * len(report.totals)
    )


def test_interval_employee_join_is_correct(labor):
    """Intervals carry the employee's number and home site from V_Employee (counts only)."""
    ivs = labor.timecards(*WEEK, 24)
    assert ivs
    assert all(i.employee_number is not None for i in ivs)
    ids = sorted({i.employee_id for i in ivs})
    home = {
        e.employee_id: e.home_site_id for e in labor.employees(employee_ids=ids, active_only=False)
    }
    assert len(home) == len(ids)
    assert all(i.home_site_id == home[i.employee_id] for i in ivs)
    assert all(i.site_id == 24 and i.site_code.startswith("ICONTN-") for i in ivs)


@pytest.mark.parametrize("site", [24, 5])
def test_timecards_equal_labor_statistics(labor, site):
    """Timecard minutes per employee and PiType equal Labor_Statistics (inclusive ThruDate)."""
    stats = labor.labor_statistics(*WEEK, site)
    ls = {(s.employee_id, s.pi_type): s.minutes for s in stats}
    tc = defaultdict(float)
    for i in labor.timecards(*WEEK, site):
        tc[(i.employee_id, i.pi_type)] += i.hours * 60
    assert {k: round(v) for k, v in tc.items()} == ls


def test_labor_statistics_site_24_week(labor):
    stats = labor.labor_statistics(*WEEK, 24)
    by_type = defaultdict(float)
    for s in stats:
        by_type[s.pi_type] += s.minutes
        assert s.dollars is None
    assert dict(by_type) == {"R": 13079.0, "O": 1729.0}


def test_labor_statistics_group_by_date_role(labor):
    stats = labor.labor_statistics(*WEEK, 24, group_by=("date", "role"))
    assert sum(s.minutes for s in stats) == 13079 + 1729
    assert all(s.punch_date is not None and s.role_id is not None for s in stats)
    assert all(s.employee_id is None for s in stats)


def test_daily_hours_matches_labor_statistics(labor, live_client):
    days = labor.daily_hours(*WEEK, 24, exclude_home_sites=None)
    assert len(days) == 7
    # independent raw SUM over the same half-open window
    df = live_client.query(
        "SELECT SUM(p.OutTime - p.InTime) AS M FROM V_PunchInterval p "
        "WHERE p.Site = ? AND p.PunchDate >= ? AND p.PunchDate < ? AND p.PiType IN ('R', 'O')",
        [24, WEEK[0], WEEK[1] + timedelta(days=1)],
    )
    raw_minutes = int(df.loc[0, "m"])
    assert raw_minutes == 13079 + 1729
    assert sum(d.total_hours for d in days) == pytest.approx(raw_minutes / 60, abs=0.01 * len(days))


def test_worked_employees_count(labor, live_client):
    workers = labor.worked_employees("2026-10-01", "2026-10-14")
    df = live_client.query(
        "SELECT COUNT(DISTINCT p.Employee) AS N FROM V_PunchInterval p "
        "WHERE p.Site IN (5, 13, 18, 20, 24, 25) AND p.PunchDate >= ? AND p.PunchDate < ?",
        [date(2026, 10, 1), date(2026, 10, 15)],
    )
    assert len(workers) == int(df.loc[0, "n"]) == 43


def test_employees_active_filter(labor, live_client):
    staff = labor.employees(24)
    assert staff
    today = business_today()
    assert all(e.enabled and (e.term_date is None or e.term_date > today) for e in staff)
    assert all(e.home_site_id == 24 for e in staff)
    df = live_client.query(
        "SELECT COUNT(*) AS N FROM V_Employee e WHERE e.HomeSite = ? AND e.Enabled = 1 "
        "AND (e.TermDate IS NULL OR e.TermDate > ?)",
        [24, today],
    )
    assert len(staff) == int(df.loc[0, "n"])
    one = labor.employees(employee_ids=[staff[0].employee_id], active_only=False)
    assert [e.employee_id for e in one] == [staff[0].employee_id]


OFFICE_WEEK = (date(2026, 9, 28), date(2026, 10, 4))  # site 13: office accounts with OPEN shifts


def test_daily_hours_office_exclusion_and_open_chains(labor):
    everyone = {
        d.punch_date: d for d in labor.daily_hours(*OFFICE_WEEK, 13, exclude_home_sites=None)
    }
    crew = {d.punch_date: d for d in labor.daily_hours(*OFFICE_WEEK, 13)}
    assert everyone.keys() == crew.keys()
    for day, d in crew.items():
        assert everyone[day].total_hours == pytest.approx(
            d.total_hours + d.excluded_hours, abs=0.02
        )
    # 09-29: the office accounts' 24:00 pieces continue as OPEN on 09-30, so the day is
    # provisional although no interval on it carries an open code.
    sep29 = everyone[date(2026, 9, 29)]
    assert sep29.provisional and sep29.open_hours > 0
    assert crew[date(2026, 9, 29)].excluded_hours > 0


def test_productivity_excludes_recharge_revenue(labor, live_client):
    rows = labor.productivity(*OFFICE_WEEK, 13)
    assert len(rows) == 7
    for r in rows:
        assert 0 <= r.recharge_revenue <= r.revenue + 0.01
        assert r.wash_revenue == pytest.approx(r.revenue - r.recharge_revenue, abs=0.01)
        if r.labor_hours:
            assert r.revenue_per_labor_hour == pytest.approx(
                r.wash_revenue / r.labor_hours, abs=0.01
            )
    # 09-28 is an ARM billing day: most of the revenue is the batched recharge.
    billing = next(r for r in rows if r.log_date == date(2026, 9, 28))
    assert billing.recharge_revenue > 0.5 * billing.revenue


def test_query_reuses_one_transaction(live_client):
    raw = live_client._conn.connection
    live_client.query("SELECT 1 AS x FROM RDB$DATABASE")
    before = len(raw.transactions)
    for _ in range(5):
        live_client.query("SELECT 1 AS x FROM RDB$DATABASE")
    assert len(raw.transactions) == before
