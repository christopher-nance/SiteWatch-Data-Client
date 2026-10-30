"""Live checks for MembershipsResource (skipped unless ``SITEWATCH_LIVE=1``).

Bounded to site 24 inside 2026-10-01..14 and run one statement at a time. Asserts
aggregates only and never prints customer values. Each method is cross-checked against an
independent query on ``V_SalePasses``/``V_PlanType``.
"""

from datetime import date

import pytest

SITE = 24
WEEK = (date(2026, 10, 5), date(2026, 10, 11))
WEEK_END_EXCL = date(2026, 10, 12)
PAID = "(f_IsCompleted(s.Status) = 1 OR f_IsIncomplete(s.Status) = 1)"


@pytest.fixture(scope="module")
def mem(live_client):
    return live_client.memberships


@pytest.fixture(scope="module")
def pass_counts(live_client):
    """Independent ``V_SalePasses`` counts by (ARM status, paid) for site 24, one week."""
    df = live_client.query(
        "SELECT sp.Status, CASE WHEN " + PAID + " THEN 1 ELSE 0 END AS Paid,"
        " COUNT(*) AS n, CAST(SUM(sp.Amount) AS NUMERIC(12,2)) AS amt"
        " FROM V_Sale s JOIN V_SalePasses sp ON sp.Site = s.Site AND sp.SaleID = s.ObjID"
        " WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ? GROUP BY 1, 2",
        [SITE, WEEK[0], WEEK_END_EXCL],
    )
    return {(int(r.status), int(r.paid)): (int(r.n), float(r.amt or 0)) for r in df.itertuples()}


def _n(counts, status, paid=None):
    return sum(v[0] for (s, p), v in counts.items() if s == status and (paid is None or p == paid))


def test_plans_match_catalog_count(mem, live_client):
    df = live_client.query(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN Enabled = 1 THEN 1 ELSE 0 END) AS e"
        " FROM V_PlanType WHERE Typ = ?",
        [110],
    )
    assert len(mem.plans()) == int(df.loc[0, "e"])
    assert len(mem.plans(include_disabled=True)) == int(df.loc[0, "n"])


def test_activity_matches_salepasses(mem, pass_counts):
    rows = mem.activity(*WEEK, SITE)
    assert rows and {r.site_code for r in rows} == {"ICONTN-030"}
    assert sum(r.joins for r in rows) == _n(pass_counts, 1, paid=1)
    assert sum(r.recharges for r in rows) == _n(pass_counts, 3, paid=1)
    assert sum(r.washes_used for r in rows) == _n(pass_counts, 0, paid=1)
    assert sum(r.declined for r in rows) == _n(pass_counts, 120)
    assert sum(r.expired for r in rows) == _n(pass_counts, 21)
    assert sum(r.discontinued for r in rows) == _n(pass_counts, 27)
    assert sum(r.terminated for r in rows) == _n(pass_counts, 29, paid=1)
    recharge_amt = pass_counts[(3, 1)][1]
    assert round(sum(r.recharge_amount for r in rows), 2) == pytest.approx(recharge_amt, abs=0.01)
    flat = mem.activity(*WEEK, SITE, by_plan=False)
    assert sum(r.joins for r in flat) == sum(r.joins for r in rows)
    assert all(r.plan_type_id is None for r in flat)


def test_two_week_tie_out(mem):
    """Spec tie-out: new members (paid rule) == roll-up joins == 60, with no NULL state."""
    start, end = date(2026, 10, 1), date(2026, 10, 14)
    totals = mem.activity_totals(start, end, SITE)
    members = mem.new_members(start, end, SITE, as_of=end)
    assert totals.joins == 60
    assert len(members) == totals.joins
    assert [m.value for m in totals.joins_by_site] == [60.0]
    assert sum(m.pass_status is None for m in members) == 0
    assert all(start <= m.join_date <= end and m.join_site_id == SITE for m in members)


def test_new_members_contact_is_opt_in(mem):
    plain = mem.new_members(*WEEK, SITE, as_of=WEEK[1])
    assert plain and all(m.contact is None for m in plain)
    full = mem.new_members(
        *WEEK, SITE, as_of=WEEK[1], include_contact=True, include_recharge_site=True
    )
    assert len(full) == len(plain)
    assert all(m.contact is not None for m in full)
    assert all(m.recharge_site_id is not None for m in full)


def test_member_status_matches_new_members(mem):
    members = mem.new_members(*WEEK, SITE, as_of=WEEK[1])
    for m in members[:3]:
        st = mem.member_status(m.customer_id, as_of=WEEK[1])
        assert st is not None and st.customer_id == m.customer_id
        assert st.plan_type_id == m.plan_type_id
        assert st.pass_status == m.pass_status
        assert st.amount == m.billing_amount
        assert st.expires == m.next_bill_date
    assert mem.member_status(999999999) is None


def test_events_match_salepasses(mem, pass_counts):
    events = mem.events(*WEEK, SITE)
    assert len(events) == sum(v[0] for v in pass_counts.values())
    total_amt = round(sum(v[1] for v in pass_counts.values()), 2)
    assert round(sum(e.amount or 0 for e in events), 2) == pytest.approx(total_amt, abs=0.01)
    assert all(e.event_at.date() == e.log_date for e in events if e.event_at)
    joins = mem.events(*WEEK, SITE, statuses=1)
    assert len(joins) == _n(pass_counts, 1)


def test_new_members_rejoins_and_join_site(live_client):
    members = live_client.memberships.new_members("2026-10-05", "2026-10-11", sites=[13, 24])
    assert members
    assert all(m.join_site_id in (13, 24) for m in members)
    assert all(m.is_rejoin == (m.prior_joins > 0) for m in members)
    first = live_client.memberships.new_members(
        "2026-10-05", "2026-10-11", sites=[13, 24], first_join_only=True
    )
    assert len(first) == sum(not m.is_rejoin for m in members)
