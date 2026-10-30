"""Live smoke tests: connection, read-only query path and guard."""

import pytest

from sitewatch_data_client import UnsafeQueryError


def test_site_lookup(live_client):
    df = live_client.query("SELECT ID, Code FROM V_Site WHERE ID = ?", [24])
    assert list(df.columns) == ["id", "code"]
    assert len(df) == 1
    assert int(df.loc[0, "id"]) == 24
    code = df.loc[0, "code"]
    assert code.startswith("ICONTN-")
    assert code == code.rstrip()


def test_max_rows_cap(live_client):
    df = live_client.query(
        "SELECT FIRST 5 ID FROM V_Site WHERE ID IN (?, ?, ?, ?, ?, ?)",
        [5, 13, 18, 20, 24, 25],
        max_rows=2,
    )
    assert len(df) == 2
    assert df.attrs["truncated"] is True


def test_guard_blocks_before_server(live_client):
    with pytest.raises(UnsafeQueryError):
        live_client.query("SELECT * FROM GEN_OBJID")
