"""Unit tests for CatalogResource (mocked connection, made-up data)."""

import math
import re

import pytest

from sitewatch_data_client import ValidationError
from sitewatch_data_client._constants import ICON_SITE_IDS
from sitewatch_data_client._safety import SENSITIVE_COLUMNS, assert_read_only_sql
from sitewatch_data_client.resources import _catalog
from sitewatch_data_client.resources._catalog import CatalogResource
from sitewatch_data_client.types._catalog import Item, ReportCategory, Terminal
from tests.conftest import make_df

ALL_SITES_DEFAULT = list(ICON_SITE_IDS)


def _sent(mock_client, call_index=-1):
    """Return (sql, params) of a call to the mocked connection."""
    call = mock_client._conn.query.call_args_list[call_index]
    sql = call.args[0]
    params = call.args[1] if len(call.args) > 1 else call.kwargs.get("params")
    return sql, params


def _item_df(**overrides):
    data = {
        "objid": [900001, 900002],
        "name": ["  Example Wash  ", "Example Monthly Pass"],
        "number": [101, 0],
        "itemtype": [11, 12],
        "itemtypename": ["Wash Service ", "Pass"],
        "basetype": [120, 140],
        "reportcategory": [102, 130],
        "category": ["Basic Washes", "ARM Sold"],
        "branch": ["001001001", "001003001"],
        "profitcenter": [1, 0],
        "ispackage": [1.0, math.nan],
        "plantype": [math.nan, 500001.0],
        "activeat": [-2147483647, 24],
        "enabled": [1, 2],
    }
    data.update(overrides)
    return make_df(data)


@pytest.fixture
def catalog(mock_client):
    return CatalogResource(mock_client)


# ---------------------------------------------------------------------------- items


class TestItems:
    def test_row_mapping(self, mock_client, catalog):
        mock_client._conn.query.return_value = _item_df()
        items = catalog.items(include_disabled=True)
        assert len(items) == 2
        a, b = items
        assert isinstance(a, Item)
        assert a.item_id == 900001
        assert a.name == "Example Wash"
        assert a.number == 101
        assert a.item_type_id == 11
        assert a.item_type == "Wash Service"
        assert a.base_type == 120
        assert a.base_type_name == "Service"
        assert a.report_category_id == 102
        assert a.report_category == "Basic Washes"
        assert a.category_branch == "001001001"
        assert a.profit_center == 1
        assert a.is_package is True
        assert a.plan_type_id is None
        assert a.active_at == -2147483647
        assert a.enabled is True

        assert b.number == 0  # 0 stays 0, it is not NULL
        assert b.base_type_name == "Prepaid"
        assert b.is_package is False  # NULL IsPackage -> False
        assert b.plan_type_id == 500001
        assert isinstance(b.plan_type_id, int)
        assert b.active_at == 24
        assert b.enabled is False  # 2 = disabled

    def test_unknown_base_type_name(self, mock_client, catalog):
        mock_client._conn.query.return_value = _item_df(basetype=[999, 140])
        assert catalog.items()[0].base_type_name == "BaseType 999"

    def test_empty(self, mock_client, catalog):
        mock_client._conn.query.return_value = make_df({})
        assert catalog.items() == []

    def test_default_filters_enabled_and_no_params(self, mock_client, catalog):
        catalog.items()
        sql, params = _sent(mock_client)
        assert "i.Enabled = 1" in sql
        assert "STARTING WITH" not in sql
        assert not params
        assert "ORDER BY rc.Branch, i.Name" in sql

    def test_include_disabled_drops_filter(self, mock_client, catalog):
        catalog.items(include_disabled=True)
        sql, _ = _sent(mock_client)
        assert "Enabled" not in sql.split("WHERE", 1)[1]

    def test_report_category_resolves_branch_then_filters(self, mock_client, catalog):
        mock_client._conn.query.side_effect = [
            make_df({"branch": ["001001"]}),
            _item_df(),
        ]
        catalog.items(report_category="  wash ")
        lookup_sql, lookup_params = _sent(mock_client, 0)
        assert "UPPER(TRIM(rc.Name)) = UPPER(?)" in lookup_sql
        assert list(lookup_params) == ["wash"]  # stripped, passed as a parameter
        assert "wash" not in lookup_sql.lower().replace("v_", "")
        sql, params = _sent(mock_client, 1)
        assert "rc.Branch STARTING WITH ?" in sql
        assert "001001" not in sql
        assert list(params) == ["001001"]

    def test_report_category_unknown(self, mock_client, catalog):
        mock_client._conn.query.return_value = make_df({"branch": []})
        with pytest.raises(ValidationError, match="Unknown report category"):
            catalog.items(report_category="Nope")
        assert mock_client._conn.query.call_count == 1

    def test_report_category_ambiguous(self, mock_client, catalog):
        mock_client._conn.query.return_value = make_df({"branch": ["001001", "001009"]})
        with pytest.raises(ValidationError, match="ambiguous"):
            catalog.items(report_category="Wash")

    @pytest.mark.parametrize("bad", ["", "   ", 5])
    def test_report_category_blank_or_wrong_type(self, mock_client, catalog, bad):
        with pytest.raises(ValidationError):
            catalog.items(report_category=bad)
        mock_client._conn.query.assert_not_called()

    def test_include_disabled_must_be_bool(self, catalog):
        with pytest.raises(ValidationError):
            catalog.items(include_disabled="yes")

    def test_injection_text_is_only_a_parameter(self, mock_client, catalog):
        evil = "x'; DELETE FROM SALE; --"
        mock_client._conn.query.return_value = make_df({"branch": []})
        with pytest.raises(ValidationError):
            catalog.items(report_category=evil)
        sql, params = _sent(mock_client)
        assert "DELETE" not in sql
        assert list(params) == [evil.strip()]


# ---------------------------------------------------------------------------- categories


class TestReportCategories:
    def test_mapping_depth_and_parent(self, mock_client, catalog):
        mock_client._conn.query.return_value = make_df(
            {
                "objid": [1, 100, 102],
                "name": ["Categories", "Wash ", "Basic Washes"],
                "branch": ["001", "001001", "001001001"],
            }
        )
        cats = catalog.report_categories()
        assert [type(c) for c in cats] == [ReportCategory] * 3
        root, wash, basic = cats
        assert (root.depth, root.parent_branch) == (1, None)
        assert (wash.name, wash.depth, wash.parent_branch) == ("Wash", 2, "001")
        assert (basic.category_id, basic.depth, basic.parent_branch) == (102, 3, "001001")

    def test_sql(self, mock_client, catalog):
        catalog.report_categories()
        sql, params = _sent(mock_client)
        assert sql == _catalog.REPORT_CATEGORIES_SQL
        assert not params

    def test_empty(self, mock_client, catalog):
        assert catalog.report_categories() == []


# ---------------------------------------------------------------------------- terminals


class TestTerminals:
    def test_mapping(self, mock_client, catalog):
        mock_client._conn.query.return_value = make_df(
            {
                "objid": [1724400000, 1724400001],
                "site": [24, 24],
                "sitecode": ["ICONTN-030", "ICONTN-030"],
                "name": ["Automatic Recharge", " Example Lane "],
                "number": [32, 9],
                "enabled": [1, 2],
            }
        )
        terms = catalog.terminals(24, include_disabled=True)
        assert all(isinstance(t, Terminal) for t in terms)
        a, b = terms
        assert (a.terminal_id, a.site_id, a.site_code, a.number, a.enabled) == (
            1724400000,
            24,
            "ICONTN-030",
            32,
            True,
        )
        assert b.name == "Example Lane"
        assert b.enabled is False

    def test_default_sites_parameterized(self, mock_client, catalog):
        catalog.terminals()
        sql, params = _sent(mock_client)
        assert "t.Site IN (?, ?, ?, ?, ?, ?)" in sql
        assert list(params) == ALL_SITES_DEFAULT
        assert "t.Enabled = 1" in sql
        for site in ALL_SITES_DEFAULT:
            assert not re.search(rf"\b{site}\b", sql)

    def test_explicit_sites_sorted_deduped(self, mock_client, catalog):
        catalog.terminals([24, 5, 24], include_disabled=True)
        sql, params = _sent(mock_client)
        assert "t.Site IN (?, ?)" in sql
        assert "Enabled" not in sql.split("WHERE", 1)[1]
        assert list(params) == [5, 24]

    @pytest.mark.parametrize("bad", [[], 0, -3, True, "24", [24, "x"]])
    def test_invalid_sites(self, mock_client, catalog, bad):
        with pytest.raises(ValidationError):
            catalog.terminals(bad)
        mock_client._conn.query.assert_not_called()

    def test_empty(self, mock_client, catalog):
        assert catalog.terminals(5) == []


# ---------------------------------------------------------------------------- safety


def test_sql_constants_have_no_sensitive_columns():
    words = set()
    for sql in _catalog.ALL_SQL:
        words |= {w.upper() for w in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", sql)}
    assert not words & set(SENSITIVE_COLUMNS)


@pytest.mark.parametrize(
    "sql",
    [
        _catalog.ITEMS_SQL.format(filters=_catalog.ITEMS_ENABLED_FILTER),
        _catalog.REPORT_CATEGORIES_SQL,
        _catalog.CATEGORY_BRANCH_LOOKUP_SQL,
        _catalog.TERMINALS_SQL.format(site_clause="t.Site IN (?)", filters=""),
    ],
)
def test_sql_passes_read_only_guard(sql):
    assert_read_only_sql(sql)
