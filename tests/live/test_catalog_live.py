"""Live checks for CatalogResource (skipped unless SITEWATCH_LIVE=1).

Catalog views are small configuration tables; each check runs one or two cheap statements
and cross-checks the method against an independent COUNT. Aggregates only.
"""

from collections import Counter
from datetime import datetime

import pytest


@pytest.fixture(scope="module")
def catalog(live_client):
    return live_client.catalog


def test_items_counts_match_independent_count(live_client, catalog):
    everything = catalog.items(include_disabled=True)
    enabled = catalog.items()
    q = live_client.query(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN Enabled = 1 THEN 1 ELSE 0 END) AS en FROM V_Item"
    )
    assert len(everything) == int(q["n"][0])
    assert len(enabled) == int(q["en"][0])
    assert all(i.enabled for i in enabled)
    assert len({i.item_id for i in everything}) == len(everything)


def test_items_base_type_mix_matches(live_client, catalog):
    everything = catalog.items(include_disabled=True)
    q = live_client.query(
        "SELECT t.BaseType, COUNT(*) AS n FROM V_Item i "
        "JOIN V_ItemType t ON t.ObjID = i.ItemType GROUP BY t.BaseType"
    )
    expected = {int(b): int(n) for b, n in zip(q["basetype"], q["n"], strict=True)}
    assert dict(Counter(i.base_type for i in everything)) == expected
    assert not [i for i in everything if i.base_type_name.startswith("BaseType ")]


def test_items_report_category_subtree(live_client, catalog):
    wash = catalog.items(report_category="wash", include_disabled=True)
    branch = next(c.branch for c in catalog.report_categories() if c.name == "Wash")
    q = live_client.query(
        "SELECT COUNT(*) AS n FROM V_Item i "
        "JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory "
        "WHERE rc.Branch STARTING WITH ?",
        [branch],
    )
    assert len(wash) == int(q["n"][0]) > 0
    assert all(i.category_branch.startswith(branch) for i in wash)


def test_report_category_tree(catalog):
    cats = catalog.report_categories()
    assert len(cats) == 52
    branches = {c.branch for c in cats}
    roots = [c for c in cats if c.parent_branch is None]
    assert len(roots) == 1 and roots[0].depth == 1
    assert all(c.parent_branch in branches for c in cats if c.parent_branch)
    assert len({c.name.upper() for c in cats}) == len(cats)


def test_terminals_site_24_match_count(live_client, catalog):
    enabled = catalog.terminals(24)
    everything = catalog.terminals(24, include_disabled=True)
    q = live_client.query(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN Enabled = 1 THEN 1 ELSE 0 END) AS en "
        "FROM V_Terminal WHERE Site = ?",
        [24],
    )
    assert len(everything) == int(q["n"][0])
    assert len(enabled) == int(q["en"][0])
    assert {t.site_code for t in everything} == {"ICONTN-030"}


def test_terminals_default_sites(catalog):
    sites = {t.site_id for t in catalog.terminals(include_disabled=True)}
    assert sites == {5, 13, 18, 20, 24, 25}


def test_sale_terminals_resolve(live_client, catalog):
    """Every terminal used by site 24 sales in one week exists in terminals()."""
    ids = {t.terminal_id for t in catalog.terminals(24, include_disabled=True)}
    q = live_client.query(
        "SELECT s.Terminal, COUNT(*) AS n FROM V_Sale s "
        "WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ? GROUP BY s.Terminal",
        [24, datetime(2026, 10, 5), datetime(2026, 10, 12)],
    )
    used = {int(t) for t in q["terminal"].dropna()}
    assert used and used <= ids
