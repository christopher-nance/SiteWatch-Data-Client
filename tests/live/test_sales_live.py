"""Live checks for SalesResource (skipped unless SITEWATCH_LIVE=1).

Bounded: one or two sites, windows inside 2026-10-05..14. Assertions use aggregate
operational figures only (no customer or employee values). The expected numbers are the
spec's verified tie-outs; they drift only if historical data is edited.
"""

from datetime import date, datetime

import pytest

SITE = 24
WEEK = (date(2026, 10, 5), date(2026, 10, 11))


@pytest.fixture(scope="module")
def sales(live_client):
    return live_client.sales


def test_daily_summary_tie_out(sales):
    rows = sales.daily_summary("2026-10-14", "2026-10-14", sites=SITE)
    assert len(rows) == 1
    r = rows[0]
    assert r.site_code == "ICONTN-030"
    assert r.completed_sales == 221
    assert r.car_count == 172
    assert r.sales_total == 2112.09
    assert r.deposits == -76.0
    assert r.revenue == 2188.09
    assert r.paidouts == -35.93
    assert r.tendered == 2112.09
    assert r.pending_wash_sales == 8
    assert r.pending_wash_total == 0.0


def test_daily_summary_matches_independent_count(sales, live_client):
    rows = sales.daily_summary(*WEEK, sites=SITE)
    df = live_client.query(
        "SELECT COUNT(*) AS n, SUM(s.Total) AS t FROM V_Sale s "
        "WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ? "
        "AND f_StatusCode(s.Status) IN ('C:P', 'C:PW')",
        [SITE, datetime(2026, 10, 5), datetime(2026, 10, 12)],
    )
    assert sum(r.completed_sales for r in rows) == int(df["n"][0])
    assert round(sum(r.sales_total for r in rows), 2) == round(float(df["t"][0]), 2)
    assert all(r.tendered == r.sales_total for r in rows)


def test_totals_by_site(sales):
    t = sales.totals(*WEEK, sites=[5, 24])
    assert [m.site_id for m in t.car_count_by_site] == [5, 24]
    assert t.car_count == sum(int(m.value) for m in t.car_count_by_site)
    assert t.revenue == round(sum(m.value for m in t.revenue_by_site), 2)


def test_hourly_sums_to_daily(sales):
    hourly = sales.hourly_summary("2026-10-14", "2026-10-14", sites=SITE)
    assert sum(h.car_count for h in hourly) == 172
    assert sum(h.completed_sales for h in hourly) == 221
    assert all(0 <= h.hour <= 23 for h in hourly)


def test_wash_lines_equal_cars_minus_pending(sales):
    rows = sales.item_sales(
        "2026-10-14", "2026-10-14", sites=SITE, group_by="category", report_category="Basic Washes"
    )
    basic = [r for r in rows if r.category_branch == "001001001"]
    assert sum(r.lines for r in basic) == 172 - 8


def test_tenders_equal_sales_total(sales):
    rows = sales.tenders("2026-10-14", "2026-10-14", sites=SITE)
    assert round(sum(r.amount for r in rows), 2) == 2112.09
    cc = [r for r in rows if r.category == "Credit Card"]
    assert round(sum(r.amount for r in cc), 2) == 2092.09


def test_list_balances(sales):
    rows = sales.list("2026-10-14", "2026-10-14", site=SITE)
    assert len(rows) == 221
    assert round(sum(s.total for s in rows), 2) == 2112.09
    assert all(abs(round(sum(ln.amount or 0 for ln in s.items), 2)) < 0.005 for s in rows)
    assert all(s.paid_at is not None and s.paid_at.hour == s.hour for s in rows)


def test_status_mix(sales):
    rows = sales.status_mix("2026-10-14", "2026-10-14", sites=SITE)
    mix = {r.status_code: r.sales for r in rows}
    assert mix == {"C:P": 57, "C:PW": 164, "I:P-W": 8, "Notes": 217}
