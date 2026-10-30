"""Tests for SitesResource with a mocked Firebird connection.

All values are made up; no real site addresses or phone numbers are used.
"""

import re
from datetime import date, datetime
from unittest.mock import patch

import pytest

from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client._safety import SENSITIVE_COLUMNS, assert_read_only_sql
from sitewatch_data_client.resources import _sites as sites_module
from sitewatch_data_client.resources._sites import SitesResource
from sitewatch_data_client.types._base import SiteWatchModel
from sitewatch_data_client.types._sites import Site, SiteFreshness
from tests.conftest import make_df

ICON = [5, 13, 18, 20, 24, 25]


def _site_df(ids, **overrides):
    data = {
        "id": list(ids),
        "code": [f"TEST-{i:03d}  " for i in ids],
        "sitename": [f"  Example Wash {i} " for i in ids],
        "enabled": [1] * len(ids),
        "address": ["100 Example St"] * len(ids),
        "address2": [""] * len(ids),
        "city": ["Exampleville"] * len(ids),
        "state": ["TN"] * len(ids),
        "zip": ["00000"] * len(ids),
        "phone": ["555-0100"] * len(ids),
    }
    data.update(overrides)
    return make_df(data)


def _sent(mock_client, call_index=-1):
    call = mock_client._conn.query.call_args_list[call_index]
    sql, params = call.args[0], call.args[1]
    return sql, list(params)


@pytest.fixture
def sites(mock_client):
    return SitesResource(mock_client)


# --- models ---------------------------------------------------------------------------------------


def test_models_subclass_base():
    assert issubclass(Site, SiteWatchModel)
    assert issubclass(SiteFreshness, SiteWatchModel)


# --- list -----------------------------------------------------------------------------------------


def test_list_defaults_to_icon_sites(mock_client, sites):
    mock_client._conn.query.return_value = _site_df(ICON)
    result = sites.list()
    sql, params = _sent(mock_client)
    assert params == ICON
    assert "st.ID IN (?, ?, ?, ?, ?, ?)" in sql
    assert "LEFT JOIN V_System y ON y.Site = st.ID" in sql
    assert "ORDER BY st.ID" in sql
    assert [s.site_id for s in result] == ICON


def test_list_maps_and_trims_row(mock_client, sites):
    mock_client._conn.query.return_value = _site_df([24])
    (site,) = sites.list(24)
    assert site == Site(
        site_id=24,
        code="TEST-024",
        name="Example Wash 24",
        enabled=True,
        address="100 Example St",
        address2=None,
        city="Exampleville",
        state="TN",
        zip="00000",
        phone="555-0100",
    )
    assert isinstance(site.site_id, int)


def test_list_disabled_and_missing_system_row(mock_client, sites):
    # LEFT JOIN with no V_System row: address columns come back None/NaN.
    mock_client._conn.query.return_value = _site_df(
        [7],
        enabled=[2],
        address=[None],
        address2=[float("nan")],
        city=[None],
        state=[None],
        zip=[None],
        phone=["   "],
    )
    (site,) = sites.list(7)
    assert site.enabled is False
    assert site.address is None and site.address2 is None
    assert site.city is None and site.state is None and site.zip is None
    assert site.phone is None


def test_list_include_office_adds_99_and_100(mock_client, sites):
    mock_client._conn.query.return_value = _site_df([*ICON, 99, 100])
    sites.list(include_office=True)
    sql, params = _sent(mock_client)
    assert params == [*ICON, 99, 100]
    assert sql.count("?") == 8


def test_list_explicit_sites_sorted_deduplicated(mock_client, sites):
    mock_client._conn.query.return_value = _site_df([3, 24])
    sites.list([24, 3, 24])
    _, params = _sent(mock_client)
    assert params == [3, 24]


def test_list_explicit_with_office_no_duplicates(mock_client, sites):
    mock_client._conn.query.return_value = _site_df([24, 99, 100])
    sites.list([24, 99], include_office=True)
    _, params = _sent(mock_client)
    assert params == [24, 99, 100]


def test_list_empty_result(mock_client, sites):
    mock_client._conn.query.return_value = make_df({})
    assert sites.list([4242]) == []


@pytest.mark.parametrize("bad", [[], 0, -1, True, "24", [24, "5"], [1.5], 3.0])
def test_list_rejects_bad_sites(mock_client, sites, bad):
    with pytest.raises(ValidationError):
        sites.list(bad)
    mock_client._conn.query.assert_not_called()


def test_list_sql_is_parameterized_not_interpolated(mock_client, sites):
    mock_client._conn.query.return_value = _site_df([31337])
    sites.list(31337)
    sql, params = _sent(mock_client)
    assert "31337" not in sql
    assert params == [31337]


# --- get ------------------------------------------------------------------------------------------


def test_get_returns_site(mock_client, sites):
    mock_client._conn.query.return_value = _site_df([24])
    site = sites.get(24)
    assert site.site_id == 24
    _, params = _sent(mock_client)
    assert params == [24]


def test_get_unknown_raises(mock_client, sites):
    mock_client._conn.query.return_value = make_df({})
    with pytest.raises(ValidationError, match="Unknown site ID: 4242"):
        sites.get(4242)


@pytest.mark.parametrize("bad", [0, -5, True, "24", None, 2.0])
def test_get_rejects_bad_id(mock_client, sites, bad):
    with pytest.raises(ValidationError):
        sites.get(bad)
    mock_client._conn.query.assert_not_called()


# --- data_freshness -----------------------------------------------------------------------------


def _freshness_side_effect(site_ids, sale, punch, rep):
    """Mock frames in call order: codes, (sale, punch) per site, replication."""
    frames = [_site_df(site_ids)]
    for sid in site_ids:
        frames.append(make_df({"latestdate": [sale.get(sid)]}))
        frames.append(make_df({"latestdate": [punch.get(sid)]}))
    frames.append(make_df({"site": list(rep.keys()), "packetapplieddate": list(rep.values())}))
    return frames


@patch.object(sites_module, "_today", return_value=date(2026, 10, 30))
def test_data_freshness_maps_and_pins_floor(_today, mock_client, sites):
    mock_client._conn.query.side_effect = _freshness_side_effect(
        [5, 24],
        sale={5: datetime(2026, 10, 15), 24: datetime(2026, 10, 14)},
        punch={5: datetime(2026, 10, 16), 24: None},
        rep={5: datetime(2026, 10, 15, 19, 32)},
    )
    result = sites.data_freshness([24, 5])

    assert result == [
        SiteFreshness(
            site_id=5,
            site_code="TEST-005",
            latest_sale_date=date(2026, 10, 15),
            latest_punch_date=date(2026, 10, 16),
            replication_applied_at=datetime(2026, 10, 15, 19, 32),
        ),
        SiteFreshness(
            site_id=24,
            site_code="TEST-024",
            latest_sale_date=date(2026, 10, 14),
            latest_punch_date=None,
            replication_applied_at=None,
        ),
    ]

    calls = mock_client._conn.query.call_args_list
    assert len(calls) == 1 + 2 * 2 + 1
    floor = datetime(2025, 9, 25)  # 2026-10-30 minus 400 days
    sale_sql, sale_params = calls[1].args[0], list(calls[1].args[1])
    punch_sql, punch_params = calls[2].args[0], list(calls[2].args[1])
    assert "MAX(s.LogDate)" in sale_sql and "FROM V_Sale s" in sale_sql
    assert "s.Site = ? AND s.LogDate >= ?" in sale_sql
    assert sale_params == [5, floor]
    assert "MAX(p.PunchDate)" in punch_sql and "V_PunchInterval" in punch_sql
    assert punch_params == [5, floor]
    assert list(calls[3].args[1]) == [24, floor]
    assert list(calls[4].args[1]) == [24, floor]
    rep_sql, rep_params = calls[5].args[0], list(calls[5].args[1])
    assert "V_Replication" in rep_sql and "r.Site IN (?, ?)" in rep_sql
    assert rep_params == [5, 24]
    for call in calls:
        upper = call.args[0].upper()
        assert "TODAY" not in upper and "CURRENT_DATE" not in upper
        assert "DESC" not in upper


@patch.object(sites_module, "_today", return_value=date(2026, 10, 30))
def test_data_freshness_defaults_to_icon(_today, mock_client, sites):
    mock_client._conn.query.side_effect = _freshness_side_effect(ICON, {}, {}, {})
    result = sites.data_freshness()
    assert [f.site_id for f in result] == ICON
    assert all(f.latest_sale_date is None for f in result)
    assert all(f.replication_applied_at is None for f in result)
    first_sql, first_params = _sent(mock_client, 0)
    assert first_params == ICON


@patch.object(sites_module, "_today", return_value=date(2026, 10, 30))
def test_data_freshness_handles_nat_and_empty_frames(_today, mock_client, sites):
    mock_client._conn.query.side_effect = [
        _site_df([24]),
        make_df({"latestdate": [float("nan")]}),
        make_df({}),
        make_df({"site": [24], "packetapplieddate": [None]}),
    ]
    (f,) = sites.data_freshness(24)
    assert f.latest_sale_date is None
    assert f.latest_punch_date is None
    assert f.replication_applied_at is None


def test_data_freshness_unknown_site_raises(mock_client, sites):
    mock_client._conn.query.return_value = _site_df([24])
    with pytest.raises(ValidationError, match="4242"):
        sites.data_freshness([24, 4242])
    assert mock_client._conn.query.call_count == 1


@pytest.mark.parametrize("bad", [[], 0, True, "24"])
def test_data_freshness_rejects_bad_sites(mock_client, sites, bad):
    with pytest.raises(ValidationError):
        sites.data_freshness(bad)
    mock_client._conn.query.assert_not_called()


# --- SQL safety -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder",
    [
        lambda: sites_module.sites_sql([5, 24]),
        lambda: sites_module.latest_sale_date_sql(24, datetime(2025, 9, 25)),
        lambda: sites_module.latest_punch_date_sql(24, datetime(2025, 9, 25)),
        lambda: sites_module.replication_sql([5, 24]),
    ],
)
def test_builders_pass_guard_and_avoid_sensitive_columns(builder):
    sql, params = builder()
    assert_read_only_sql(sql)
    words = {w.upper() for w in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", sql)}
    assert not (words & SENSITIVE_COLUMNS)
    assert "*" not in sql
    assert sql.count("?") == len(params)
