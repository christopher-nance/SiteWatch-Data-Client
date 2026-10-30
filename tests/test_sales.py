"""Tests for SalesResource with a mocked Firebird connection.

All values are made up. No real customer or employee data is used.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from sitewatch_data_client import ValidationError
from sitewatch_data_client._constants import ICON_SITE_IDS
from sitewatch_data_client.resources import _sales as sales_mod
from sitewatch_data_client.resources._sales import SalesResource
from sitewatch_data_client.types._sales import (
    DailySales,
    HourlySales,
    ItemSales,
    Sale,
    SaleLine,
    SalesTotals,
    StatusCount,
    TenderTotal,
)
from tests.conftest import make_df

D14 = datetime(2026, 10, 14)
D15 = datetime(2026, 10, 15)

SENSITIVE = ("SSNUM", "PWORD", "SECURITYCODE", "DRVLNUM", "MASKEDPAN", "NOTE", "PLATE")


def _codes(*pairs):
    return make_df({"id": [p[0] for p in pairs], "code": [p[1] for p in pairs]})


@pytest.fixture
def sales(mock_client):
    return SalesResource(mock_client)


def _calls(mock_client):
    """List of (sql, params) sent to the connection."""
    return [(c.args[0], list(c.args[1])) for c in mock_client._conn.query.call_args_list]


def _assert_parameterized(sql, params, *values):
    for v in values:
        assert v in params
    # no user value formatted into the SQL text
    assert "2026" not in sql
    assert "ICONTN" not in sql


# ---------------------------------------------------------------------------
# daily_summary
# ---------------------------------------------------------------------------


def _daily_frames():
    headers = make_df(
        {
            "site": [24, 24],
            "logdate": [D14, D14],
            "iscompleted": [1, 0],
            "sales": [221, 8],
            "salestotal": [2112.090000000001, 0.0],
        }
    )
    cars = make_df({"site": [24], "logdate": [D14], "carcount": [172.0]})
    lines = make_df(
        {
            "site": [24, 24, 24, 24],
            "logdate": [D14] * 4,
            "basetype": [150, 160, 170, 180],
            "amt": [-2637.0, Decimal("-35.93"), -76.0, -2112.0900000000006],
        }
    )
    return [headers, cars, lines, _codes((24, "ICONTN-030"))]


class TestDailySummary:
    def test_tie_out_mapping(self, mock_client, sales):
        mock_client._conn.query.side_effect = _daily_frames()
        rows = sales.daily_summary("2026-10-14", "2026-10-14", sites=24)

        assert len(rows) == 1
        r = rows[0]
        assert isinstance(r, DailySales)
        assert r.site_id == 24
        assert r.site_code == "ICONTN-030"
        assert r.log_date == date(2026, 10, 14)
        assert r.completed_sales == 221
        assert r.car_count == 172
        assert r.sales_total == 2112.09  # float noise rounded away
        assert r.deposits == -76.0
        assert r.revenue == 2188.09
        assert r.paidouts == -35.93
        assert r.discounts == -2637.0
        assert r.tendered == 2112.09
        assert r.pending_wash_sales == 8
        assert r.pending_wash_total == 0.0

    def test_sql_is_parameterized(self, mock_client, sales):
        mock_client._conn.query.side_effect = _daily_frames()
        sales.daily_summary(date(2026, 10, 14), date(2026, 10, 14), sites=[24])
        calls = _calls(mock_client)
        assert len(calls) == 4
        (h_sql, h_params), (c_sql, c_params), (l_sql, l_params), (s_sql, s_params) = calls

        assert "f_IsIncomplete(s.Status) = 1" in h_sql
        assert "GROUP BY s.Site, s.LogDate, 3" in h_sql
        assert h_params == [24, D14, D15]  # end + 1 day, half-open
        assert "s.LogDate >= ? AND s.LogDate < ?" in h_sql

        assert "V_SaleFacts" in c_sql and "f.Name = ?" in c_sql
        assert c_params == [24, D14, D15, 1, "TotalCarCount"]

        assert "t.BaseType IN (?, ?, ?, ?)" in l_sql
        assert l_params == [24, D14, D15, 150, 160, 170, 180]

        assert "V_Site" in s_sql and s_params == [24]
        for sql, params in calls:
            _assert_parameterized(sql, params)

    def test_default_sites_are_icon(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({})] * 3
        assert sales.daily_summary("2026-10-01", "2026-10-02") == []
        h_sql, h_params = _calls(mock_client)[0]
        assert h_params[: len(ICON_SITE_IDS)] == list(ICON_SITE_IDS)
        assert h_sql.count("?") == len(ICON_SITE_IDS) + 2

    def test_empty_result_skips_site_lookup(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({})] * 3
        assert sales.daily_summary("2026-10-01", "2026-10-01", sites=5) == []
        assert mock_client._conn.query.call_count == 3

    def test_car_only_day_and_ordering(self, mock_client, sales):
        d13 = datetime(2026, 10, 13)
        mock_client._conn.query.side_effect = [
            make_df(
                {
                    "site": [24, 5],
                    "logdate": [D14, D14],
                    "iscompleted": [1, 1],
                    "sales": [3, 2],
                    "salestotal": [30.0, 20.0],
                }
            ),
            make_df({"site": [24], "logdate": [d13], "carcount": [1.0]}),
            make_df({}),
            _codes((5, "ICONTN-005"), (24, "ICONTN-030")),
        ]
        rows = sales.daily_summary("2026-10-13", "2026-10-14", sites=[24, 5])
        assert [(r.site_id, r.log_date.day) for r in rows] == [(5, 14), (24, 13), (24, 14)]
        assert rows[1].completed_sales == 0 and rows[1].car_count == 1
        assert rows[0].revenue == 20.0 and rows[0].tendered == 0.0

    def test_range_limit(self, sales):
        with pytest.raises(ValidationError, match="31-day limit"):
            sales.daily_summary("2026-09-01", "2026-10-14")

    def test_inverted_range(self, sales):
        with pytest.raises(ValidationError):
            sales.daily_summary("2026-10-14", "2026-10-01")

    @pytest.mark.parametrize("bad", [[], 0, -1, True, "24", [24, "x"]])
    def test_invalid_sites(self, sales, bad):
        with pytest.raises(ValidationError):
            sales.daily_summary("2026-10-14", "2026-10-14", sites=bad)

    def test_validation_happens_before_query(self, mock_client, sales):
        with pytest.raises(ValidationError):
            sales.daily_summary("not-a-date", "2026-10-14")
        mock_client._conn.query.assert_not_called()


# ---------------------------------------------------------------------------
# totals
# ---------------------------------------------------------------------------


class TestTotals:
    def test_sums_and_by_site(self, mock_client, sales):
        d13 = datetime(2026, 10, 13)
        mock_client._conn.query.side_effect = [
            make_df(
                {
                    "site": [5, 5, 24],
                    "logdate": [d13, D14, D14],
                    "iscompleted": [1, 1, 1],
                    "sales": [10, 20, 5],
                    "salestotal": [100.1, 200.2, 50.0],
                }
            ),
            make_df({"site": [5, 24], "logdate": [D14, D14], "carcount": [7.0, 3.0]}),
            make_df({"site": [24], "logdate": [D14], "basetype": [170], "amt": [-10.0]}),
            _codes((5, "ICONTN-005"), (13, "ICONTN-015"), (24, "ICONTN-030")),
        ]
        t = sales.totals("2026-10-13", "2026-10-14", sites=[24, 5, 13])
        assert isinstance(t, SalesTotals)
        assert t.period_start == date(2026, 10, 13)
        assert t.period_end == date(2026, 10, 14)
        assert t.completed_sales == 35
        assert t.car_count == 10
        assert t.sales_total == 350.3
        assert t.revenue == 360.3
        assert [(m.site_id, m.site_code, m.value) for m in t.car_count_by_site] == [
            (5, "ICONTN-005", 7.0),
            (13, "ICONTN-015", 0.0),
            (24, "ICONTN-030", 3.0),
        ]
        assert [(m.site_id, m.value) for m in t.revenue_by_site] == [
            (5, 300.3),
            (13, 0.0),
            (24, 60.0),
        ]

    def test_empty(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({})] * 3 + [_codes((24, "ICONTN-030"))]
        t = sales.totals("2026-10-14", "2026-10-14", sites=24)
        assert t.completed_sales == 0 and t.car_count == 0 and t.revenue == 0.0
        assert t.car_count_by_site[0].value == 0.0

    def test_range_limit(self, sales):
        with pytest.raises(ValidationError):
            sales.totals("2026-08-01", "2026-10-14")


# ---------------------------------------------------------------------------
# hourly_summary
# ---------------------------------------------------------------------------


class TestHourlySummary:
    def test_merge(self, mock_client, sales):
        mock_client._conn.query.side_effect = [
            make_df(
                {
                    "site": [24, 24],
                    "logdate": [D14, D14],
                    "hr": [6, 9],
                    "completedsales": [50, 12],
                    "salestotal": [1500.0000000001, 120.0],
                }
            ),
            make_df(
                {"site": [24, 24], "logdate": [D14, D14], "hr": [9, 10], "carcount": [11.0, 4.0]}
            ),
            _codes((24, "ICONTN-030")),
        ]
        rows = sales.hourly_summary("2026-10-14", "2026-10-14", sites=24)
        assert all(isinstance(r, HourlySales) for r in rows)
        assert [(r.hour, r.completed_sales, r.car_count, r.sales_total) for r in rows] == [
            (6, 50, 0, 1500.0),
            (9, 12, 11, 120.0),
            (10, 0, 4, 0.0),
        ]
        assert rows[0].site_code == "ICONTN-030"

    def test_sql(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({}), make_df({})]
        assert sales.hourly_summary("2026-10-14", "2026-10-14", sites=[24, 5]) == []
        (s_sql, s_params), (c_sql, c_params) = _calls(mock_client)
        assert "ss.Hr" in s_sql and "f_IsCompleted(ss.Status) = 1" in s_sql
        assert s_params == [5, 24, D14, D15]
        assert c_params == [5, 24, D14, D15, 1, "TotalCarCount"]
        _assert_parameterized(s_sql, s_params)

    def test_range_limit_14(self, sales):
        with pytest.raises(ValidationError, match="14-day limit"):
            sales.hourly_summary("2026-10-01", "2026-10-15")


# ---------------------------------------------------------------------------
# item_sales
# ---------------------------------------------------------------------------


def _item_df():
    return make_df(
        {
            "site": [24, 24, 24],
            "logdate": [D14, D14, D14],
            "itemid": [1001, 1002, 1003],
            "itemname": ["Example Wash  ", "Header Line", "Example Tender"],
            "category": ["Basic Washes", "Basic Wash Headers", "Credit Card"],
            "branch": ["001001001", "001001001001", "002001"],
            "basetype": [120, 120, 180],
            "lines": [10, 4, 3],
            "qty": [10.0, 0.0, 3.0],
            "amount": [180.0, 0.0, -54.000000001],
        }
    )


class TestItemSales:
    def test_mapping_drops_zero_rows(self, mock_client, sales):
        mock_client._conn.query.side_effect = [_item_df(), _codes((24, "ICONTN-030"))]
        rows = sales.item_sales("2026-10-14", "2026-10-14", sites=24)
        assert all(isinstance(r, ItemSales) for r in rows)
        assert [r.item_id for r in rows] == [1001, 1003]
        wash = rows[0]
        assert wash.item_name == "Example Wash"
        assert wash.category_branch == "001001001"
        assert wash.base_type_name == "Service"
        assert rows[1].amount == -54.0 and rows[1].base_type_name == "Tender"

    def test_include_zero(self, mock_client, sales):
        mock_client._conn.query.side_effect = [_item_df(), _codes((24, "ICONTN-030"))]
        rows = sales.item_sales("2026-10-14", "2026-10-14", sites=24, include_zero=True)
        assert len(rows) == 3

    def test_group_by_category_sql_and_fields(self, mock_client, sales):
        df = make_df(
            {
                "site": [24],
                "logdate": [D14],
                "category": ["Basic Washes"],
                "branch": ["001001001"],
                "basetype": [120],
                "lines": [10],
                "qty": [10.0],
                "amount": [180.0],
            }
        )
        mock_client._conn.query.side_effect = [df, _codes((24, "ICONTN-030"))]
        rows = sales.item_sales("2026-10-14", "2026-10-14", sites=24, group_by="category")
        sql, params = _calls(mock_client)[0]
        assert "i.ObjID" not in sql.split("FROM")[0] and "rc.Branch" in sql
        assert params == [24, D14, D15]
        assert rows[0].item_id is None and rows[0].item_name is None
        assert rows[0].category == "Basic Washes"

    def test_group_by_base_type(self, mock_client, sales):
        df = make_df(
            {
                "site": [24],
                "logdate": [D14],
                "basetype": [150],
                "lines": [5],
                "qty": [5.0],
                "amount": [-25.0],
            }
        )
        mock_client._conn.query.side_effect = [df, _codes((24, "ICONTN-030"))]
        rows = sales.item_sales("2026-10-14", "2026-10-14", sites=24, group_by="base_type")
        sql, _ = _calls(mock_client)[0]
        assert "rc.Name" not in sql.split("FROM")[0]
        assert rows[0].category is None and rows[0].category_branch is None
        assert rows[0].base_type_name == "CouponDisc"

    def test_report_category_resolved_and_parameterized(self, mock_client, sales):
        mock_client._conn.query.side_effect = [
            make_df({"branch": ["001001001"]}),
            _item_df(),
            _codes((24, "ICONTN-030")),
        ]
        sales.item_sales("2026-10-14", "2026-10-14", sites=24, report_category="  basic washes ")
        (l_sql, l_params), (i_sql, i_params), _ = _calls(mock_client)
        assert "UPPER(TRIM(rc.Name)) = UPPER(?)" in l_sql
        assert l_params == ["basic washes"]
        assert "rc.Branch STARTING WITH ?" in i_sql
        assert i_params == [24, D14, D15, "001001001"]
        assert "001001001" not in i_sql and "basic" not in i_sql.lower()

    def test_unknown_report_category(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({})]
        with pytest.raises(ValidationError, match="Unknown report category"):
            sales.item_sales("2026-10-14", "2026-10-14", report_category="Nope")

    def test_ambiguous_report_category(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({"branch": ["001", "002"]})]
        with pytest.raises(ValidationError, match="ambiguous"):
            sales.item_sales("2026-10-14", "2026-10-14", report_category="Dup")

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_blank_report_category(self, sales, bad):
        with pytest.raises(ValidationError):
            sales.item_sales("2026-10-14", "2026-10-14", report_category=bad)

    def test_bad_group_by(self, mock_client, sales):
        with pytest.raises(ValidationError, match="group_by"):
            sales.item_sales("2026-10-14", "2026-10-14", group_by="gl")
        mock_client._conn.query.assert_not_called()

    def test_empty(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({})]
        assert sales.item_sales("2026-10-14", "2026-10-14") == []


# ---------------------------------------------------------------------------
# tenders
# ---------------------------------------------------------------------------


class TestTenders:
    def test_category_level(self, mock_client, sales):
        df = make_df(
            {
                "site": [24, 24],
                "logdate": [D14, D14],
                "category": ["House Accounts", "Credit Card"],
                "branch": ["002003", "002001"],
                "lines": [2, 150],
                "amount": [28.0, 2092.0900000001],
            }
        )
        mock_client._conn.query.side_effect = [df, _codes((24, "ICONTN-030"))]
        rows = sales.tenders("2026-10-14", "2026-10-14", sites=24)
        assert all(isinstance(r, TenderTotal) for r in rows)
        assert [(r.category, r.amount, r.tender) for r in rows] == [
            ("Credit Card", 2092.09, None),
            ("House Accounts", 28.0, None),
        ]
        sql, params = _calls(mock_client)[0]
        assert "-SUM(COALESCE(si.Amt, 0))" in sql and "t.BaseType = ?" in sql
        assert "JOIN V_Tender" not in sql and "i.ObjID" not in sql.split("FROM")[0]
        assert params == [24, D14, D15, 180]

    def test_by_item(self, mock_client, sales):
        df = make_df(
            {
                "site": [24, 24],
                "logdate": [D14, D14],
                "category": ["Credit Card", "Credit Card"],
                "branch": ["002001", "002001"],
                "itemid": [11, 12],
                "itemname": ["Visa / MC", "Example Card"],
                "lines": [10, 1],
                "amount": [100.0, 12.0],
            }
        )
        mock_client._conn.query.side_effect = [df, _codes((24, "ICONTN-030"))]
        rows = sales.tenders("2026-10-14", "2026-10-14", sites=24, by_item=True)
        assert [r.tender for r in rows] == ["Example Card", "Visa / MC"]
        sql, _ = _calls(mock_client)[0]
        assert "GROUP BY s.Site, s.LogDate, rc.Name, rc.Branch, i.ObjID, i.Name" in sql

    def test_default_sites(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({})]
        assert sales.tenders("2026-10-14", "2026-10-14") == []
        _, params = _calls(mock_client)[0]
        assert params[:6] == list(ICON_SITE_IDS)

    def test_range_limit(self, sales):
        with pytest.raises(ValidationError):
            sales.tenders("2026-09-01", "2026-10-14")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _header_df():
    return make_df(
        {
            "site": [24, 24],
            "objid": [500, 400],
            "receiptnumber": [123456, 654321],
            "code": ["", "A1"],
            "statuscode": ["C:PW", "C:P"],
            "iscompleted": [1, 1],
            "created": [datetime(2026, 10, 14, 9, 0), None],
            "logdate": [D14, D14],
            "paidat": [datetime(2026, 10, 14, 9, 5), datetime(2026, 10, 14, 6, 20)],
            "hr": [9, 6],
            "shift": [1, None],
            "terminalid": [7, None],
            "terminalname": ["XPT1", None],
            "customercodeid": [None, 9001],
            "customerid": [None, 8001],
            "actualsaleid": [None, 300],
            "total": [18.0, 29.990000000001],
        }
    )


def _lines_df():
    return make_df(
        {
            "saleid": [500, 500, 500, 400],
            "seq": [2, 1, 3, 1],
            "leader": [1, None, None, None],
            "item": [1, 2, 3, 4],
            "itemname": ["Example Wash", "Example Pkg", "Visa / MC", "Example Plan"],
            "category": ["Basic Washes", "Basic Wash Headers", "Credit Card", "ARM"],
            "basetype": [120, 120, 180, 140],
            "qty": [1.0, 1.0, 1.0, None],
            "val": [18.0, 0.0, None, 29.99],
            "amt": [18.0, None, -18.0, 29.99],
        }
    )


class TestList:
    def test_mapping(self, mock_client, sales):
        mock_client._conn.query.side_effect = [
            _header_df(),
            _lines_df(),
            _codes((24, "ICONTN-030")),
        ]
        rows = sales.list("2026-10-14", "2026-10-14", site=24)
        assert all(isinstance(r, Sale) for r in rows)
        first, second = rows
        assert first.sale_id == 500 and first.site_code == "ICONTN-030"
        assert first.receipt_number == "123456"
        assert first.code is None  # empty text becomes None
        assert first.status_code == "C:PW" and first.is_completed is True
        assert first.paid_at == datetime(2026, 10, 14, 9, 5)
        assert first.log_date == date(2026, 10, 14)
        assert first.hour == 9 and first.terminal_name == "XPT1"
        assert first.customer_id is None
        assert [ln.seq for ln in first.items] == [1, 2, 3]
        assert all(isinstance(ln, SaleLine) for ln in first.items)
        pkg, wash, tender = first.items
        assert pkg.amount is None and pkg.leader_seq is None
        assert wash.leader_seq == 1 and wash.amount == 18.0
        assert tender.is_tender is True and tender.unit_value is None

        assert second.created_at is None and second.shift is None
        assert second.terminal_id is None
        assert second.customer_code_id == 9001 and second.customer_id == 8001
        assert second.actual_sale_id == 300
        assert second.total == 29.99
        assert second.items[0].quantity is None

    def test_sql_completed_only_by_default(self, mock_client, sales):
        mock_client._conn.query.side_effect = [_header_df(), _lines_df(), _codes((24, "X"))]
        sales.list(date(2026, 10, 13), date(2026, 10, 14), site=24)
        (h_sql, h_params), (l_sql, l_params), _ = _calls(mock_client)
        assert h_params == [24, datetime(2026, 10, 13), D15]
        assert l_params == h_params
        assert "f_IsCompleted(s.Status) = 1" in h_sql and "f_IsCompleted(s.Status) = 1" in l_sql
        assert "LogDate + s.LogTime / 14400.0" in h_sql
        assert "f_SaleScr(s.ObjID)" in h_sql
        assert "LEFT JOIN V_Terminal" in h_sql and "LEFT JOIN V_CustomerCode" in h_sql
        for sql in (h_sql, l_sql):
            upper = sql.upper()
            assert "SI.NOTE" not in upper and "CC.CODE" not in upper
            _assert_parameterized(sql, [])

    def test_include_incomplete_no_items(self, mock_client, sales):
        mock_client._conn.query.side_effect = [_header_df(), _codes((24, "X"))]
        rows = sales.list(
            "2026-10-14", "2026-10-14", site=24, include_items=False, include_incomplete=True
        )
        assert mock_client._conn.query.call_count == 2
        h_sql, _ = _calls(mock_client)[0]
        assert "f_IsCompleted(s.Status) = 1" not in h_sql
        assert all(r.items == [] for r in rows)

    def test_empty(self, mock_client, sales):
        mock_client._conn.query.side_effect = [make_df({})]
        assert sales.list("2026-10-14", "2026-10-14", site=24) == []
        assert mock_client._conn.query.call_count == 1

    def test_range_limit_3(self, sales):
        with pytest.raises(ValidationError, match="3-day limit"):
            sales.list("2026-10-10", "2026-10-13", site=24)

    @pytest.mark.parametrize("bad", [None, 0, True, [24], "24"])
    def test_site_required_int(self, sales, bad):
        with pytest.raises(ValidationError):
            sales.list("2026-10-14", "2026-10-14", site=bad)


# ---------------------------------------------------------------------------
# status_mix
# ---------------------------------------------------------------------------


class TestStatusMix:
    def test_mapping(self, mock_client, sales):
        df = make_df(
            {
                "site": [24, 24, 24],
                "logdate": [D14, D14, D14],
                "statuscode": ["Notes", "C:PW", "I:P-W"],
                "iscompleted": [0, 1, 0],
                "sales": [217, 164, 8],
                "total": [6570.72, 514.0, None],
            }
        )
        mock_client._conn.query.side_effect = [df, _codes((24, "ICONTN-030"))]
        rows = sales.status_mix("2026-10-14", "2026-10-14", sites=24)
        assert all(isinstance(r, StatusCount) for r in rows)
        assert [(r.status_code, r.is_completed, r.sales, r.total) for r in rows] == [
            ("C:PW", True, 164, 514.0),
            ("I:P-W", False, 8, 0.0),
            ("Notes", False, 217, 6570.72),
        ]
        sql, params = _calls(mock_client)[0]
        assert "GROUP BY s.Site, s.LogDate, 3, 4" in sql
        assert "f_IsCompleted(s.Status) = 1" not in sql
        assert params == [24, D14, D15]

    def test_range_limit_14(self, sales):
        with pytest.raises(ValidationError):
            sales.status_mix("2026-10-01", "2026-10-15")


# ---------------------------------------------------------------------------
# shared behaviour
# ---------------------------------------------------------------------------


def test_site_code_lookup_is_cached(mock_client, sales):
    status_df = make_df(
        {
            "site": [24],
            "logdate": [D14],
            "statuscode": ["C:P"],
            "iscompleted": [1],
            "sales": [1],
            "total": [1.0],
        }
    )
    mock_client._conn.query.side_effect = [status_df, _codes((24, "ICONTN-030")), status_df]
    sales.status_mix("2026-10-14", "2026-10-14", sites=24)
    rows = sales.status_mix("2026-10-14", "2026-10-14", sites=24)
    assert rows[0].site_code == "ICONTN-030"
    assert mock_client._conn.query.call_count == 3


def test_money_rounding_helpers():
    assert sales_mod._money(2112.090000000001) == 2112.09
    assert sales_mod._money(1837.0200000000011) == 1837.02
    assert sales_mod._money(Decimal("-35.93")) == -35.93
    assert sales_mod._money(None) == 0.0
    assert sales_mod._money(float("nan")) == 0.0
    assert str(sales_mod._money(-0.0001)) == "0.0"
    assert sales_mod._opt_money(None) is None


def test_builders_never_select_sensitive_columns():
    builders = [
        sales_mod.daily_headers_sql([24], D14, D15),
        sales_mod.daily_car_count_sql([24], D14, D15),
        sales_mod.daily_base_types_sql([24], D14, D15),
        sales_mod.hourly_sales_sql([24], D14, D15),
        sales_mod.hourly_car_count_sql([24], D14, D15),
        sales_mod.item_sales_sql([24], D14, D15, "item", "001"),
        sales_mod.tenders_sql([24], D14, D15, True),
        sales_mod.sale_headers_sql(24, D14, D15, True),
        sales_mod.sale_lines_sql(24, D14, D15, True),
        sales_mod.status_mix_sql([24], D14, D15),
        sales_mod.site_codes_sql([24]),
        sales_mod.category_branch_sql("x"),
    ]
    for sql, _ in builders:
        upper = sql.upper()
        for word in SENSITIVE:
            assert f".{word}" not in upper and f" {word}," not in upper
        assert "SELECT *" not in upper and ".*" not in upper
        assert "LIMIT" not in upper
        assert "IN (SELECT" not in upper


def test_builders_pass_guard():
    from sitewatch_data_client._safety import assert_read_only_sql

    for sql, _ in [
        sales_mod.daily_headers_sql([5, 24], D14, D15),
        sales_mod.item_sales_sql([24], D14, D15, "category", "001"),
        sales_mod.tenders_sql([24], D14, D15, False),
        sales_mod.sale_headers_sql(24, D14, D15, False),
        sales_mod.sale_lines_sql(24, D14, D15, False),
        sales_mod.status_mix_sql([24], D14, D15),
    ]:
        assert_read_only_sql(sql)
