"""Tests for MembershipsResource with mocked DataFrames (made-up values only)."""

import re
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client.resources._memberships import (
    MembershipsResource,
    activity_sql,
    events_sql,
    member_status_sql,
    new_members_sql,
    plans_sql,
)
from tests.conftest import make_df

ICON = [5, 13, 18, 20, 24, 25]
SENSITIVE = ("SSNUM", "PWORD", "SECURITYCODE", "DRVLNUM", "MASKEDPAN", "EXPIRATION", "BIRTHDATE")


def _has_sensitive(sql: str) -> bool:
    return any(re.search(rf"\b{c}\b", sql, re.IGNORECASE) for c in SENSITIVE)


@pytest.fixture
def res(mock_client):
    """MembershipsResource with the site-code cache pre-seeded (no lookup query)."""
    r = MembershipsResource(mock_client)
    r._site_code_cache.update({5: "ICONTN-005", 13: "ICONTN-015", 24: "ICONTN-030"})
    return r


def _call(mock_client, index=-1):
    """(sql, params) of a recorded ``_conn.query`` call."""
    args = mock_client._conn.query.call_args_list[index].args
    return args[0], list(args[1])


def _activity_row(**over):
    row = {
        "site": 24,
        "logdate": pd.Timestamp("2026-10-05"),
        "plantype": 901,
        "planname": "Unlimited Example ",
        "joins": 2,
        "joinamount": 39.98000000000001,
        "recharges": 10,
        "rechargeamount": Decimal("299.90"),
        "upsells": 1,
        "upsellamount": 5.0,
        "washesused": 30,
        "transfersin": 0,
        "transfersout": 0,
        "suspended": 0,
        "resumed": 0,
        "terminated": 1,
        "terminatedamount": -19.99,
        "expired": 1,
        "discontinuing": 2,
        "discontinued": 1,
        "declined": 12,
        "cardexpiring": 0,
        "rechargeproblems": None,
    }
    row.update(over)
    return row


def _df(rows):
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------------------------


class TestPlans:
    def test_maps_rows(self, res, mock_client):
        mock_client._conn.query.return_value = make_df(
            {
                "objid": [901, 902],
                "name": ["Unlimited Example  ", "Family Example"],
                "enabled": [1, 2],
                "expirationage": [0, 9999],
                "rechargeplan": [0, 903],
                "modified": [pd.Timestamp("2026-01-02 03:04:05"), pd.NaT],
            }
        )
        plans = res.plans(include_disabled=True)
        assert [p.plan_type_id for p in plans] == [901, 902]
        assert plans[0].name == "Unlimited Example"
        assert plans[0].enabled is True and plans[1].enabled is False
        assert plans[0].recharge_plan_id is None
        assert plans[1].recharge_plan_id == 903
        assert plans[0].modified_at == datetime(2026, 1, 2, 3, 4, 5)
        assert plans[1].modified_at is None

    def test_enabled_filter_and_params(self, res, mock_client):
        res.plans()
        sql, params = _call(mock_client)
        assert "pt.Typ = ?" in sql and "pt.Enabled = 1" in sql
        assert params == [110]
        res.plans(include_disabled=True)
        sql, _ = _call(mock_client)
        assert "Enabled = 1" not in sql

    def test_empty(self, res, mock_client):
        mock_client._conn.query.return_value = make_df({})
        assert res.plans() == []


# ---------------------------------------------------------------------------------------------
# activity / activity_totals
# ---------------------------------------------------------------------------------------------


class TestActivity:
    def test_maps_rows_and_rounds_money(self, res, mock_client):
        mock_client._conn.query.return_value = _df([_activity_row()])
        (row,) = res.activity("2026-10-05", "2026-10-11", 24)
        assert row.site_id == 24 and row.site_code == "ICONTN-030"
        assert row.log_date == date(2026, 10, 5)
        assert row.plan_type_id == 901 and row.plan_name == "Unlimited Example"
        assert row.joins == 2 and row.join_amount == 39.98
        assert row.recharge_amount == 299.9 and isinstance(row.recharge_amount, float)
        assert row.terminated_amount == -19.99
        assert row.recharge_problems == 0

    def test_params_half_open_and_default_sites(self, res, mock_client):
        res.activity(date(2026, 10, 5), date(2026, 10, 11))
        sql, params = _call(mock_client)
        assert "a.Site IN (?, ?, ?, ?, ?, ?)" in sql
        assert "a.LogDate >= ? AND a.LogDate < ?" in sql
        assert params == [*ICON, datetime(2026, 10, 5), datetime(2026, 10, 12)]
        assert "2026" not in sql

    def test_by_plan_false(self, res, mock_client):
        row = _activity_row()
        del row["plantype"], row["planname"]
        mock_client._conn.query.return_value = _df([row])
        (out,) = res.activity("2026-10-05", "2026-10-05", 24, by_plan=False)
        assert out.plan_type_id is None and out.plan_name is None
        sql, _ = _call(mock_client)
        assert "V_PlanType" not in sql and "PlanType" not in sql.split("FROM")[0]

    def test_range_limit(self, res, mock_client):
        res.activity("2026-07-01", "2026-10-01", 24)  # 93 days
        with pytest.raises(ValidationError, match="93-day"):
            res.activity("2026-07-01", "2026-10-02", 24)

    @pytest.mark.parametrize("bad", [0, -1, True, "24", [], [24, "x"]])
    def test_invalid_sites(self, res, bad):
        with pytest.raises(ValidationError):
            res.activity("2026-10-05", "2026-10-11", bad)

    def test_invalid_dates(self, res):
        with pytest.raises(ValidationError):
            res.activity("2026-10-11", "2026-10-05")
        with pytest.raises(ValidationError):
            res.activity("10/05/2026", "2026-10-11")

    def test_empty_skips_site_lookup(self, mock_client):
        r = MembershipsResource(mock_client)
        assert r.activity("2026-10-05", "2026-10-11") == []
        assert mock_client._conn.query.call_count == 1

    def test_site_code_lookup_is_cached(self, mock_client):
        r = MembershipsResource(mock_client)
        mock_client._conn.query.side_effect = [
            _df([_activity_row()]),
            make_df({"id": [24], "code": ["ICONTN-030  "]}),
            _df([_activity_row()]),
        ]
        assert r.activity("2026-10-05", "2026-10-05", 24)[0].site_code == "ICONTN-030"
        sql, params = _call(mock_client, 1)
        assert sql.startswith("SELECT ID, Code FROM V_Site WHERE ID IN (?)")
        assert params == [24]
        r.activity("2026-10-05", "2026-10-05", 24)
        assert mock_client._conn.query.call_count == 3


class TestActivityTotals:
    def test_sums_and_by_site(self, res, mock_client):
        rows = [
            _activity_row(site=5, joins=3, recharges=4, rechargeamount=100.005),
            _activity_row(site=24, joins=2, recharges=6, rechargeamount=200.0),
            _activity_row(
                site=24,
                logdate=pd.Timestamp("2026-10-06"),
                joins=1,
                recharges=0,
                rechargeamount=0.0,
            ),
        ]
        for r in rows:
            del r["plantype"], r["planname"]
        mock_client._conn.query.return_value = _df(rows)
        t = res.activity_totals("2026-10-05", "2026-10-06", [24, 5, 13])
        assert t.period_start == date(2026, 10, 5) and t.period_end == date(2026, 10, 6)
        assert t.joins == 6 and t.recharges == 10
        assert t.recharge_amount == 300.0
        assert t.declined == 36 and t.terminated == 3
        assert [(m.site_id, m.site_code, m.value) for m in t.joins_by_site] == [
            (5, "ICONTN-005", 3.0),
            (13, "ICONTN-015", 0.0),
            (24, "ICONTN-030", 3.0),
        ]
        sql, params = _call(mock_client)
        assert "V_PlanType" not in sql
        assert params[:3] == [5, 13, 24]

    def test_empty(self, res, mock_client):
        t = res.activity_totals("2026-10-05", "2026-10-06", 24)
        assert t.joins == 0 and t.recharge_amount == 0.0
        assert [(m.site_id, m.value) for m in t.joins_by_site] == [(24, 0.0)]


# ---------------------------------------------------------------------------------------------
# new_members
# ---------------------------------------------------------------------------------------------


def _member_row(**over):
    row = {
        "customer": 100001,
        "joindate": pd.Timestamp("2026-10-05"),
        "joinsite": 24,
        "firstsalesite": 24,
        "plantype": 901,
        "planname": "Unlimited Example",
        "passstatus": 1,
        "passstatusname": "Joined ",
        "billingamount": Decimal("19.99"),
        "expires": pd.Timestamp("2026-11-05"),
        "membersince": pd.Timestamp("2026-10-05"),
        "members": 1,
        "lastused": pd.Timestamp("2026-10-09"),
        "usessincejoin": 3,
        "lastcharge": pd.Timestamp("2026-10-05"),
    }
    row.update(over)
    return row


_CONTACT = {
    "firstname": "Pat",
    "lastname": "Example",
    "fullname": "Pat Example",
    "company": "",
    "address1": "1 Test Way",
    "address2": None,
    "city": "Testville",
    "state": "TN",
    "zipcode": "00000",
    "mainphone": "555-0100",
    "altphone": None,
    "email": "pat@example.com",
    "lastcustcode": "TEST123",
    "suppressmail": 0,
    "customercreated": pd.Timestamp("2026-10-05 08:00:00"),
    "codes": "TEST123 TAG999",
}


class TestNewMembers:
    def test_maps_rows_without_contact(self, res, mock_client):
        mock_client._conn.query.return_value = _df(
            [
                _member_row(),
                _member_row(
                    customer=100002,
                    passstatus=None,
                    passstatusname=None,
                    billingamount=None,
                    expires=pd.NaT,
                    lastused=None,
                    usessincejoin=0,
                    plantype=None,
                ),
            ]
        )
        a, b = res.new_members("2026-10-05", "2026-10-11", 24)
        assert a.customer_id == 100001 and a.join_date == date(2026, 10, 5)
        assert a.join_site_id == 24 and a.join_site_code == "ICONTN-030"
        assert a.pass_status == 1 and a.pass_status_name == "Joined"
        assert a.billing_amount == 19.99 and isinstance(a.billing_amount, float)
        assert a.next_bill_date == date(2026, 11, 5)
        assert a.member_since == date(2026, 10, 5)
        assert a.last_used_date == date(2026, 10, 9) and a.uses_since_join == 3
        assert a.contact is None and a.recharge_site_id is None
        assert b.pass_status is None and b.billing_amount is None
        assert b.next_bill_date is None and b.last_used_date is None and b.plan_type_id is None

    def test_sql_has_no_pii_by_default(self, res, mock_client):
        res.new_members("2026-10-05", "2026-10-11", 24)
        sql, _ = _call(mock_client)
        upper = sql.upper()
        for col in ("V_CUSTOMER ", "EMAIL", "MAINPHONE", "ADDRESS1", "V_CUSTOMERCODE"):
            assert col not in upper
        assert not _has_sensitive(sql)
        assert "RECHARGESITE" not in upper

    def test_params_order_and_as_of_end_of_day(self, res, mock_client):
        res.new_members("2026-10-05", "2026-10-11", [24, 5], as_of=date(2026, 10, 11))
        sql, params = _call(mock_client)
        window = [110, 5, 24, datetime(2026, 10, 5), datetime(2026, 10, 12)]
        # JoinSite subquery, PriorJoins subquery (Typ), derived table, as_of.
        assert params == [*window, 110, *window, datetime(2026, 10, 11, 23, 59, 59)]
        assert sql.count("?") == len(params)
        assert "Arm_SalePass_Member(j.Customer, ?)" in sql
        assert "f_IsIncomplete(s.Status) = 1" in sql

    def test_join_site_is_earliest_join_and_prior_joins_counted(self, res, mock_client):
        res.new_members("2026-10-05", "2026-10-11", [24, 5])
        sql, _ = _call(mock_client)
        flat = " ".join(sql.split())
        assert "MIN(p.Site)" not in flat
        assert "ORDER BY s5.LogDate, s5.LogTime, p5.Site) AS JoinSite" in flat
        assert "JOIN V_Sale s5 ON s5.Site = p5.Site AND s5.ObjID = p5.SaleID" in flat
        assert "s6.LogDate < j.JoinDate) AS PriorJoins" in flat
        assert "JOIN V_Sale s6 ON s6.Site = p6.Site AND s6.ObjID = p6.SaleID" in flat
        assert "p6.Customer = j.Customer AND p6.Status = 1 AND pt6.Typ = ?" in flat

    def test_rejoin_fields_and_first_join_only(self, res, mock_client):
        mock_client._conn.query.return_value = _df(
            [_member_row(priorjoins=0), _member_row(customer=100002, priorjoins=3)]
        )
        a, b = res.new_members("2026-10-05", "2026-10-11", 24)
        assert (a.prior_joins, a.is_rejoin) == (0, False)
        assert (b.prior_joins, b.is_rejoin) == (3, True)
        (only,) = res.new_members("2026-10-05", "2026-10-11", 24, first_join_only=True)
        assert only.customer_id == 100001

    def test_as_of_none_sends_null_and_default_sites(self, res, mock_client):
        res.new_members("2026-10-05", "2026-10-11")
        _, params = _call(mock_client)
        assert params[1:7] == ICON
        assert params[-1] is None

    def test_as_of_datetime_and_string(self, res, mock_client):
        res.new_members("2026-10-05", "2026-10-11", 24, as_of=datetime(2026, 10, 11, 6, 0))
        assert _call(mock_client)[1][-1] == datetime(2026, 10, 11, 23, 59, 59)
        res.new_members("2026-10-05", "2026-10-11", 24, as_of="2026-10-14")
        assert _call(mock_client)[1][-1] == datetime(2026, 10, 14, 23, 59, 59)
        with pytest.raises(ValidationError):
            res.new_members("2026-10-05", "2026-10-11", 24, as_of="yesterday")

    def test_include_contact(self, res, mock_client):
        mock_client._conn.query.return_value = _df([_member_row(**_CONTACT)])
        (m,) = res.new_members("2026-10-05", "2026-10-11", 24, include_contact=True)
        sql, _ = _call(mock_client)
        assert "V_Customer c" in sql and "V_CustomerCode" in sql
        assert not _has_sensitive(sql)
        c = m.contact
        assert c.first_name == "Pat" and c.full_name == "Pat Example"
        assert c.company is None and c.zip == "00000"
        assert c.codes == ["TEST123", "TAG999"] and c.last_code == "TEST123"
        assert c.suppress_mail is False
        assert c.customer_created_at == datetime(2026, 10, 5, 8, 0)

    def test_include_recharge_site(self, res, mock_client):
        mock_client._conn.query.return_value = _df([_member_row(rechargesite=13)])
        (m,) = res.new_members("2026-10-05", "2026-10-11", 24, include_recharge_site=True)
        sql, _ = _call(mock_client)
        assert "AS RechargeSite" in sql
        assert m.recharge_site_id == 13 and m.recharge_site_code == "ICONTN-015"

    def test_range_limit(self, res):
        res.new_members("2026-09-01", "2026-10-01", 24)  # 31 days
        with pytest.raises(ValidationError, match="31-day"):
            res.new_members("2026-09-01", "2026-10-02", 24)

    def test_empty(self, res, mock_client):
        assert res.new_members("2026-10-05", "2026-10-11", 24) == []


# ---------------------------------------------------------------------------------------------
# member_status
# ---------------------------------------------------------------------------------------------


class TestMemberStatus:
    def test_maps_row(self, res, mock_client):
        mock_client._conn.query.return_value = _df(
            [
                {
                    "leader": 100001,
                    "member": 100001,
                    "site": 24,
                    "saleid": 555,
                    "plantype": 901,
                    "planname": "Unlimited Example",
                    "passstatus": 3,
                    "passstatusname": "Renewed",
                    "salestatuscode": "C:P ",
                    "salepaidts": pd.Timestamp("2026-10-05 10:30:00"),
                    "washessold": 999,
                    "washesused": 4,
                    "washesleft": 995,
                    "expires": pd.Timestamp("2026-11-05"),
                    "firstsalesite": 24,
                    "membersince": pd.Timestamp("2025-01-01"),
                    "members": 2,
                    "amount": Decimal("29.99"),
                    "upsell": 0.0,
                }
            ]
        )
        st = res.member_status(100001, as_of="2026-10-14")
        assert st.customer_id == 100001 and st.sale_id == 555
        assert st.pass_status_name == "Renewed" and st.sale_status_code == "C:P"
        assert st.sale_paid_at == datetime(2026, 10, 5, 10, 30)
        assert st.expires == date(2026, 11, 5) and st.family_members == 2
        assert st.amount == 29.99 and st.upsell == 0.0
        sql, params = _call(mock_client)
        assert "Arm_SalePass_Member(?, ?)" in sql
        assert params == [100001, datetime(2026, 10, 14, 23, 59, 59)]

    def test_none_when_no_row(self, res, mock_client):
        assert res.member_status(100001) is None
        assert _call(mock_client)[1] == [100001, None]

    def test_none_when_all_null_row(self, res, mock_client):
        mock_client._conn.query.return_value = _df(
            [{"leader": None, "member": None, "saleid": None, "plantype": None, "passstatus": None}]
        )
        assert res.member_status(100001) is None

    @pytest.mark.parametrize("bad", [0, -5, True, "100001", 1.5, None])
    def test_invalid_customer(self, res, bad):
        with pytest.raises(ValidationError):
            res.member_status(bad)


# ---------------------------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------------------------


def _event_row(**over):
    row = {
        "site": 24,
        "saleid": 555,
        "seq": 0,
        "logdate": pd.Timestamp("2026-10-05"),
        "eventat": pd.Timestamp("2026-10-05 09:15:00"),
        "salestatuscode": "C:PW",
        "customer": 100001,
        "leader": 100001,
        "plantype": 901,
        "planname": "Unlimited Example",
        "status": 1,
        "statusname": "Joined",
        "washes": 999,
        "expires": pd.Timestamp("2026-11-05"),
        "members": 1,
        "amount": 19.990000000000002,
        "upsell": None,
        "firstsalesite": 24,
    }
    row.update(over)
    return row


class TestEvents:
    def test_maps_rows_and_keeps_duplicates(self, res, mock_client):
        mock_client._conn.query.return_value = _df(
            [_event_row(), _event_row(status=0, statusname="Used", amount=0.0)]
        )
        a, b = res.events("2026-10-05", "2026-10-11", 24)
        assert (a.sale_id, b.sale_id) == (555, 555)
        assert a.site_code == "ICONTN-030" and a.event_at == datetime(2026, 10, 5, 9, 15)
        assert a.amount == 19.99 and a.upsell is None
        assert a.status_name == "Joined" and b.status == 0
        assert a.expires == date(2026, 11, 5)

    def test_params_and_status_filter(self, res, mock_client):
        res.events("2026-10-05", "2026-10-11", 24, statuses=[130, 120, 120])
        sql, params = _call(mock_client)
        assert "s.Site = ? AND s.LogDate >= ? AND s.LogDate < ? AND sp.Status IN (?, ?)" in sql
        assert params == [24, datetime(2026, 10, 5), datetime(2026, 10, 12), 120, 130]
        res.events("2026-10-05", "2026-10-11", 24, statuses=1)
        assert _call(mock_client)[1][-1] == 1
        res.events("2026-10-05", "2026-10-11", 24)
        sql, params = _call(mock_client)
        assert "sp.Status IN" not in sql and len(params) == 3

    @pytest.mark.parametrize("bad", [[], ["1"], [True], [-1], "1", 1.0])
    def test_invalid_statuses(self, res, bad):
        with pytest.raises(ValidationError):
            res.events("2026-10-05", "2026-10-11", 24, statuses=bad)

    @pytest.mark.parametrize("bad", [None, 0, [24], True])
    def test_site_required(self, res, bad):
        with pytest.raises(ValidationError):
            res.events("2026-10-05", "2026-10-11", bad)

    def test_range_limit(self, res):
        res.events("2026-10-05", "2026-10-11", 24)
        with pytest.raises(ValidationError, match="7-day"):
            res.events("2026-10-05", "2026-10-12", 24)

    def test_empty(self, res, mock_client):
        assert res.events("2026-10-05", "2026-10-11", 24) == []


# ---------------------------------------------------------------------------------------------
# SQL safety
# ---------------------------------------------------------------------------------------------


class TestSqlBuilders:
    def test_no_sensitive_columns_and_no_in_select(self):
        built = [
            plans_sql(True),
            activity_sql(ICON, datetime(2026, 10, 1), datetime(2026, 10, 2), True),
            new_members_sql(ICON, datetime(2026, 10, 1), datetime(2026, 10, 2), None, True, True),
            member_status_sql(1, None),
            events_sql(24, datetime(2026, 10, 1), datetime(2026, 10, 2), [1, 3]),
        ]
        for sql, params in built:
            upper = sql.upper()
            assert not _has_sensitive(sql)
            assert "IN (SELECT" not in upper
            assert "SELECT *" not in upper
            assert sql.count("?") == len(params)
