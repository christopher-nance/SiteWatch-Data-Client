"""Integration tests: resource wiring, public exports and labor.productivity (no database)."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

import sitewatch_data_client as swdc
from sitewatch_data_client import (
    CatalogResource,
    DailyLaborHours,
    DailyProductivity,
    DailySales,
    LaborResource,
    MembershipsResource,
    SalesResource,
    SitesResource,
    SiteWatchClient,
    SiteWatchModel,
    ValidationError,
)
from sitewatch_data_client.types import _memberships, _sales
from sitewatch_data_client.types._common import SiteMetric
from tests.conftest import make_df

RESOURCES = {
    "sites": SitesResource,
    "sales": SalesResource,
    "catalog": CatalogResource,
    "memberships": MembershipsResource,
    "labor": LaborResource,
}


@pytest.mark.parametrize("name,cls", RESOURCES.items())
def test_resource_is_cached_property(mock_client, name, cls):
    first = getattr(mock_client, name)
    assert isinstance(first, cls)
    assert getattr(mock_client, name) is first
    assert first._client is mock_client
    assert getattr(SiteWatchClient, name).__doc__


def test_resources_do_not_connect_on_access():
    client = SiteWatchClient(
        host="test-host", database="test.fdb", user="u", password="p", fb_client=None
    )
    for name in RESOURCES:
        getattr(client, name)
    assert client._conn is None


def test_all_exports_resolve():
    for name in swdc.__all__:
        assert hasattr(swdc, name), name
    assert len(swdc.__all__) == len(set(swdc.__all__))


def test_every_model_exported_and_is_sitewatch_model():
    import sitewatch_data_client.types as t

    for name in t.__all__:
        obj = getattr(t, name)
        assert name in swdc.__all__, name
        if isinstance(obj, type) and issubclass(obj, SiteWatchModel):
            continue
        # Enums (PunchSource, TimecardFlag) are the only non-model exports.
        assert issubclass(obj, str), name


def test_site_metric_defined_once():
    assert _sales.SiteMetric is SiteMetric
    assert _memberships.SiteMetric is SiteMetric
    assert swdc.SiteMetric is SiteMetric


def _sale(site, day, cars, revenue, code="ICONTN-999"):
    return DailySales(
        site_id=site,
        site_code=code,
        log_date=day,
        completed_sales=cars,
        car_count=cars,
        sales_total=revenue,
        deposits=0.0,
        revenue=revenue,
        paidouts=0.0,
        discounts=0.0,
        tendered=revenue,
        pending_wash_sales=0,
        pending_wash_total=0.0,
    )


def _hours(site, day, hours, open_punches=0, code="ICONTN-999"):
    return DailyLaborHours(
        site_id=site,
        site_code=code,
        punch_date=day,
        employees=3,
        regular_hours=hours,
        overtime_hours=0.0,
        total_hours=hours,
        open_punches=open_punches,
    )


def test_productivity_merges_sales_and_hours(mock_client):
    d1, d2, d3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
    sales = MagicMock(
        return_value=[_sale(24, d1, 200, 1500.0), _sale(24, d2, 100, 800.0), _sale(5, d1, 50, 300)]
    )
    hours = MagicMock(
        return_value=[_hours(24, d1, 40.0), _hours(24, d3, 8.0, open_punches=1), _hours(5, d1, 0.0)]
    )
    mock_client.sales.daily_summary = sales
    mock_client.labor.daily_hours = hours

    rows = mock_client.labor.productivity("2026-10-05", "2026-10-07", sites=[24, 5])

    assert all(isinstance(r, DailyProductivity) for r in rows)
    assert [(r.site_id, r.log_date) for r in rows] == [(5, d1), (24, d1), (24, d2), (24, d3)]
    by_key = {(r.site_id, r.log_date): r for r in rows}
    r = by_key[(24, d1)]
    assert (r.car_count, r.labor_hours, r.cars_per_labor_hour, r.revenue_per_labor_hour) == (
        200,
        40.0,
        5.0,
        37.5,
    )
    # sales but no punches
    assert by_key[(24, d2)].labor_hours == 0.0
    assert by_key[(24, d2)].cars_per_labor_hour is None
    # punches but no sales, OPEN carried through
    assert by_key[(24, d3)].car_count == 0
    assert by_key[(24, d3)].open_punches == 1
    assert by_key[(24, d3)].provisional is True
    assert by_key[(24, d1)].provisional is False
    assert by_key[(24, d3)].cars_per_labor_hour == 0.0
    # zero hours -> None ratios
    assert by_key[(5, d1)].revenue_per_labor_hour is None
    # both sides receive the same normalized sites
    assert sales.call_args.kwargs["sites"] == hours.call_args.kwargs["sites"] == [5, 24]


def test_productivity_excludes_auto_recharge_revenue(mock_client):
    d1 = date(2026, 10, 5)
    mock_client.sales.daily_summary = MagicMock(return_value=[_sale(24, d1, 400, 12000.0)])
    mock_client.labor.daily_hours = MagicMock(return_value=[_hours(24, d1, 40.0)])
    mock_client._conn.query.return_value = make_df(
        {"site": [24], "logdate": [datetime(2026, 10, 5)], "rechargetotal": [11000.0]}
    )
    [r] = mock_client.labor.productivity("2026-10-05", "2026-10-05", sites=24)
    assert (r.revenue, r.recharge_revenue, r.wash_revenue) == (12000.0, 11000.0, 1000.0)
    assert r.revenue_per_labor_hour == 25.0
    assert r.cars_per_labor_hour == 10.0
    sql, params = mock_client._conn.query.call_args.args
    assert "JOIN V_Terminal t ON t.ObjID = s.Terminal AND t.Site = s.Site" in sql
    assert "f_IsCompleted(s.Status) = 1" in sql
    assert params == [24, datetime(2026, 10, 5), datetime(2026, 10, 6), "Automatic Recharge"]
    # office accounts are excluded from the hours by default
    assert mock_client.labor.daily_hours.call_args.kwargs["exclude_home_sites"] == (99,)


def test_productivity_range_limit(mock_client):
    mock_client.sales.daily_summary = MagicMock()
    with pytest.raises(ValidationError):
        mock_client.labor.productivity("2026-09-01", "2026-10-14")
    mock_client.sales.daily_summary.assert_not_called()
    mock_client._conn.query.assert_not_called()
