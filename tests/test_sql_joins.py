"""Join-key snapshot tests for every library SQL builder.

The other unit tests check placeholders, parameters and a few substrings. These pin every
``JOIN ... ON ...`` clause exactly (whitespace-normalized), so swapping a join key (for
example ``e.ObjID = p.Site``) or dropping the ``Site`` half of a composite
``Site + SaleID`` key fails a test instead of silently returning wrong rows.
"""

import re
from collections import Counter
from datetime import datetime

import pytest

from sitewatch_data_client.resources import _catalog, _labor, _memberships, _sales, _sites

D1 = datetime(2026, 10, 1)
D2 = datetime(2026, 10, 2)
SITES = [5, 24]

_JOIN = re.compile(
    r"\b((?:LEFT\s+)?JOIN\s+.+?\s+ON\s+.+?)"
    r"(?=\s+(?:LEFT\s+)?JOIN\b|\s+WHERE\b|\s+GROUP\s+BY\b|\s+ORDER\s+BY\b|\)|$)",
    re.IGNORECASE,
)


def joins(sql: str) -> Counter:
    """Every JOIN clause in ``sql`` with whitespace collapsed."""
    flat = " ".join(sql.split())
    return Counter(m.group(1) for m in _JOIN.finditer(flat))


SALE_ITEMS = [
    "JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID",
    "JOIN V_Item i ON i.ObjID = si.Item",
    "JOIN V_ItemType t ON t.ObjID = i.ItemType",
]
REPORT_CATEGORY = "JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory"

CASES = {
    # --- sales ---------------------------------------------------------------------------
    "sales.daily_headers": (_sales.daily_headers_sql(SITES, D1, D2)[0], []),
    "sales.daily_car_count": (
        _sales.daily_car_count_sql(SITES, D1, D2)[0],
        [
            "JOIN V_SaleFacts sf ON sf.Site = s.Site AND sf.SaleID = s.ObjID",
            "JOIN V_Fact f ON f.ObjID = sf.Fact",
        ],
    ),
    "sales.daily_base_types": (_sales.daily_base_types_sql(SITES, D1, D2)[0], SALE_ITEMS),
    "sales.daily_auto_recharge": (
        _sales.daily_auto_recharge_sql(SITES, D1, D2)[0],
        ["JOIN V_Terminal t ON t.ObjID = s.Terminal AND t.Site = s.Site"],
    ),
    "sales.hourly_sales": (
        _sales.hourly_sales_sql(SITES, D1, D2)[0],
        ["JOIN V_Sale s ON s.Site = ss.Site AND s.ObjID = ss.SaleID"],
    ),
    "sales.hourly_car_count": (
        _sales.hourly_car_count_sql(SITES, D1, D2)[0],
        [
            "JOIN V_SaleFacts sf ON sf.Site = ss.Site AND sf.SaleID = ss.SaleID",
            "JOIN V_Fact f ON f.ObjID = sf.Fact",
        ],
    ),
    "sales.item_sales": (
        _sales.item_sales_sql(SITES, D1, D2, "item")[0],
        [*SALE_ITEMS, REPORT_CATEGORY],
    ),
    "sales.tenders": (_sales.tenders_sql(SITES, D1, D2)[0], [*SALE_ITEMS, REPORT_CATEGORY]),
    "sales.sale_headers": (
        _sales.sale_headers_sql(24, D1, D2)[0],
        [
            "JOIN V_SaleStats ss ON ss.Site = s.Site AND ss.SaleID = s.ObjID",
            "LEFT JOIN V_Terminal t ON t.ObjID = s.Terminal",
            "LEFT JOIN V_CustomerCode cc ON cc.ObjID = s.CustomerCode",
        ],
    ),
    "sales.sale_lines": (_sales.sale_lines_sql(24, D1, D2)[0], [*SALE_ITEMS, REPORT_CATEGORY]),
    "sales.status_mix": (_sales.status_mix_sql(SITES, D1, D2)[0], []),
    # --- catalog -------------------------------------------------------------------------
    "catalog.items": (
        _catalog.ITEMS_SQL,
        [
            "JOIN V_ItemType t ON t.ObjID = i.ItemType",
            REPORT_CATEGORY,
        ],
    ),
    "catalog.terminals": (_catalog.TERMINALS_SQL, ["LEFT JOIN V_Site st ON st.ID = t.Site"]),
    # --- sites ---------------------------------------------------------------------------
    "sites.list": (_sites.sites_sql(SITES)[0], ["LEFT JOIN V_System y ON y.Site = st.ID"]),
    # --- memberships ---------------------------------------------------------------------
    "memberships.activity": (
        _memberships.activity_sql(SITES, D1, D2)[0],
        ["LEFT JOIN V_PlanType pt ON pt.ObjID = a.PlanType"],
    ),
    "memberships.new_members": (
        _memberships.new_members_sql(
            SITES, D1, D2, None, include_contact=True, include_recharge_site=True
        )[0],
        [
            # JoinSite (earliest in-window join) and PriorJoins subqueries
            "JOIN V_Sale s5 ON s5.Site = p5.Site AND s5.ObjID = p5.SaleID",
            "JOIN V_PlanType pt5 ON pt5.ObjID = p5.PlanType",
            "JOIN V_Sale s6 ON s6.Site = p6.Site AND s6.ObjID = p6.SaleID",
            "JOIN V_PlanType pt6 ON pt6.ObjID = p6.PlanType",
            # LastUsed and UsesSinceJoin
            "JOIN V_Sale s2 ON s2.Site = p2.Site AND s2.ObjID = p2.SaleID",
            "JOIN V_Sale s2 ON s2.Site = p2.Site AND s2.ObjID = p2.SaleID",
            # LastCharge and RechargeSite
            "JOIN V_Sale s3 ON s3.Site = p3.Site AND s3.ObjID = p3.SaleID",
            "JOIN V_Sale s4 ON s4.Site = p4.Site AND s4.ObjID = p4.SaleID",
            # derived join table
            "JOIN V_Sale s ON s.Site = p.Site AND s.ObjID = p.SaleID",
            "JOIN V_PlanType pt ON pt.ObjID = p.PlanType",
            "LEFT JOIN V_Customer c ON c.ObjID = j.Customer",
            "LEFT JOIN Arm_SalePass_Member(j.Customer, ?) m ON 1 = 1",
            "LEFT JOIN V_PlanType cp ON cp.ObjID = m.PlanType",
        ],
    ),
    "memberships.member_status": (
        _memberships.member_status_sql(100001, None)[0],
        ["LEFT JOIN V_PlanType pt ON pt.ObjID = m.PlanType"],
    ),
    "memberships.events": (
        _memberships.events_sql(24, D1, D2)[0],
        [
            "JOIN V_SalePasses sp ON sp.Site = s.Site AND sp.SaleID = s.ObjID",
            "JOIN V_PlanType pt ON pt.ObjID = sp.PlanType",
        ],
    ),
    # --- labor ---------------------------------------------------------------------------
    "labor.employees": (
        _labor.employees_sql(SITES, employee_ids=None, active_as_of=None)[0],
        ["LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category"],
    ),
    "labor.worked_employees": (
        _labor.worked_employees_sql(SITES, D1, D2)[0],
        [
            "JOIN V_Employee e ON e.ObjID = w.Employee",
            "LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category",
        ],
    ),
    "labor.timecards": (
        _labor.timecards_sql(SITES, D1, D2, ["R", "O"])[0],
        [
            "JOIN V_Site s ON s.ID = p.Site",
            "LEFT JOIN V_Employee e ON e.ObjID = p.Employee",
            "LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category",
            "LEFT JOIN V_EmpRole r ON r.ObjID = p.Department",
        ],
    ),
    "labor.daily_hours": (
        _labor.daily_hours_sql(SITES, D1, D2)[0],
        [
            "JOIN V_Site s ON s.ID = p.Site",
            "LEFT JOIN V_Employee e ON e.ObjID = p.Employee",
        ],
    ),
    "labor.labor_statistics": (
        _labor.labor_statistics_sql(D1, D1, 24, [None, 1, None], [None] * 4, include_dollars=False)[
            0
        ],
        [
            "LEFT JOIN V_Employee e ON e.ObjID = ls.Employee",
            "LEFT JOIN V_EmpRole r ON r.ObjID = ls.Role",
        ],
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_join_clauses_exact(name):
    sql, expected = CASES[name]
    assert joins(sql) == Counter(expected)


def test_new_members_without_options_drops_optional_joins():
    sql, _ = _memberships.new_members_sql(SITES, D1, D2, None)
    found = joins(sql)
    assert "LEFT JOIN V_Customer c ON c.ObjID = j.Customer" not in found
    assert "JOIN V_Sale s4 ON s4.Site = p4.Site AND s4.ObjID = p4.SaleID" not in found


def test_activity_without_plan_has_no_join():
    assert joins(_memberships.activity_sql(SITES, D1, D2, by_plan=False)[0]) == Counter()


def test_parser_sees_a_mutated_key():
    good = "FROM V_Sale s JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID WHERE 1=1"
    bad = good.replace("si.Site = s.Site AND ", "")
    assert joins(good) != joins(bad)
