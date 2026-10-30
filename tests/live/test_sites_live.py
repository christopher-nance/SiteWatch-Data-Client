"""Live checks for SitesResource (skipped unless ``SITEWATCH_LIVE=1``).

Bounded: two 59-row master views, and freshness probes for at most two sites. Each result
is cross-checked against an independent statement. Asserts aggregates and site codes only.
"""

from datetime import date, datetime, timedelta

import pytest

from sitewatch_data_client import ICON_ALL_SITE_IDS, ICON_SITE_IDS, ValidationError


@pytest.fixture(scope="module")
def sites(live_client):
    return live_client.sites


def test_list_default_is_icon_and_matches_v_site(sites, live_client):
    result = sites.list()
    assert [s.site_id for s in result] == list(ICON_SITE_IDS)
    assert all(s.code.startswith("ICONTN-") for s in result)
    assert all(s.enabled for s in result)
    assert all(s.name and s.name.lower() != "system" for s in result)

    df = live_client.query(
        "SELECT ID, Code, SiteName FROM V_Site WHERE ID IN (?, ?, ?, ?, ?, ?) ORDER BY ID",
        list(ICON_SITE_IDS),
    )
    expected = [(int(r["id"]), r["code"], r["sitename"]) for r in df.to_dict("records")]
    assert expected == [(s.site_id, s.code, s.name) for s in result]


def test_list_address_matches_v_system(sites, live_client):
    result = sites.list(include_office=True)
    assert [s.site_id for s in result] == sorted(ICON_ALL_SITE_IDS)
    df = live_client.query(
        "SELECT Site, City, State, Zip FROM V_System WHERE Site IN (?, ?, ?, ?, ?, ?, ?, ?) "
        "ORDER BY Site",
        sorted(ICON_ALL_SITE_IDS),
    )
    expected = [
        (int(r["site"]), r["city"] or None, r["state"] or None, r["zip"] or None)
        for r in df.to_dict("records")
    ]
    assert expected == [(s.site_id, s.city, s.state, s.zip) for s in result]


def test_get_known_and_unknown(sites):
    assert sites.get(24).code == "ICONTN-030"
    assert sites.get(13).code == "ICONTN-015"  # the code is not the site ID
    with pytest.raises(ValidationError):
        sites.get(4242)


def test_data_freshness_matches_independent_probes(sites, live_client):
    result = sites.data_freshness([5, 24])
    assert [f.site_id for f in result] == [5, 24]
    floor = date.today() - timedelta(days=400)
    for f in result:
        assert f.site_code.startswith("ICONTN-")
        assert f.latest_sale_date is not None and f.latest_sale_date >= floor
        assert f.latest_punch_date is not None and f.latest_punch_date >= floor
        sale_dt = datetime.combine(f.latest_sale_date, datetime.min.time())
        punch_dt = datetime.combine(f.latest_punch_date, datetime.min.time())
        after_sale = live_client.query(
            "SELECT COUNT(*) AS n FROM V_Sale s WHERE s.Site = ? AND s.LogDate > ?",
            [f.site_id, sale_dt],
        )
        on_sale = live_client.query(
            "SELECT COUNT(*) AS n FROM V_Sale s WHERE s.Site = ? AND s.LogDate = ?",
            [f.site_id, sale_dt],
        )
        after_punch = live_client.query(
            "SELECT COUNT(*) AS n FROM V_PunchInterval p WHERE p.Site = ? AND p.PunchDate > ?",
            [f.site_id, punch_dt],
        )
        assert int(after_sale.loc[0, "n"]) == 0
        assert int(on_sale.loc[0, "n"]) > 0
        assert int(after_punch.loc[0, "n"]) == 0


def test_data_freshness_replication_matches_view(sites, live_client):
    # Car-wash sites carry NULL packet dates; the hub (99) carries the packet time.
    result = {f.site_id: f for f in sites.data_freshness([24, 99])}
    df = live_client.query(
        "SELECT Site, PacketAppliedDate FROM V_Replication WHERE Site IN (?, ?) ORDER BY Site",
        [24, 99],
    )
    for r in df.to_dict("records"):
        raw = r["packetapplieddate"]
        got = result[int(r["site"])].replication_applied_at
        if raw is None or raw != raw:  # None or NaT
            assert got is None
        else:
            assert got == (raw.to_pydatetime() if hasattr(raw, "to_pydatetime") else raw)
    assert result[24].replication_applied_at is None
    assert result[99].replication_applied_at is not None
