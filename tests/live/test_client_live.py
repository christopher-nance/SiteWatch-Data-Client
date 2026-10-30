"""Live checks for the SiteWatchClient core: config, read-only transactions and ``query()``.

Skipped unless ``SITEWATCH_LIVE=1`` (see ``tests/live/conftest.py``). Bounded to Site 24 and
the pay week 2026-10-05..2026-10-11; asserts aggregates only.
"""

import os
from datetime import date, datetime

import pytest

from sitewatch_data_client import QueryError, SiteWatchClient, UnsafeQueryError

SITE = 24
START = date(2026, 10, 5)
END_EXCLUSIVE = date(2026, 10, 12)

_COMPLETED_FILTER = (
    "FROM V_Sale WHERE Site = ? AND LogDate >= ? AND LogDate < ? AND f_IsCompleted(Status) = 1"
)


def test_transaction_is_read_only(live_client):
    """Firebird itself reports the per-query transaction as read-only, read-committed."""
    df = live_client.query(
        "SELECT MON$READ_ONLY AS ro, MON$ISOLATION_MODE AS iso FROM MON$TRANSACTIONS "
        "WHERE MON$TRANSACTION_ID = CURRENT_TRANSACTION"
    )
    assert len(df) == 1
    assert int(df.loc[0, "ro"]) == 1
    # 2 = read committed record version, 3 = read committed no record version
    assert int(df.loc[0, "iso"]) in (2, 3)


def test_each_query_gets_a_new_transaction(live_client):
    sql = "SELECT CURRENT_TRANSACTION AS tx FROM RDB$DATABASE"
    first = int(live_client.query(sql).loc[0, "tx"])
    second = int(live_client.query(sql).loc[0, "tx"])
    assert second != first


def test_row_fetch_ties_out_to_server_count_and_sum(live_client):
    """Rows fetched with max_rows=None equal an independent server-side COUNT/SUM."""
    params = [SITE, START, END_EXCLUSIVE]
    agg = live_client.query(
        f"SELECT COUNT(*) AS n, CAST(SUM(Total) AS NUMERIC(12,2)) AS tot {_COMPLETED_FILTER}",
        params,
    )
    rows = live_client.query(f"SELECT ObjID, Total {_COMPLETED_FILTER}", params, max_rows=None)
    n = int(agg.loc[0, "n"])
    assert n > 0
    assert len(rows) == n
    assert rows.attrs["truncated"] is False
    assert rows["objid"].is_unique
    assert round(float(rows["total"].sum()), 2) == round(float(agg.loc[0, "tot"]), 2)


def test_date_and_midnight_datetime_params_match(live_client):
    sql = f"SELECT COUNT(*) AS n {_COMPLETED_FILTER}"
    by_date = live_client.query(sql, [SITE, START, END_EXCLUSIVE])
    by_dt = live_client.query(sql, [SITE, datetime(2026, 10, 5), datetime(2026, 10, 12)])
    assert int(by_date.loc[0, "n"]) == int(by_dt.loc[0, "n"])


def test_default_cap_not_hit_for_small_result(live_client):
    df = live_client.query(
        f"SELECT FIRST 20 ObjID {_COMPLETED_FILTER}", [SITE, START, END_EXCLUSIVE]
    )
    assert len(df) == 20
    assert df.attrs["truncated"] is False


def test_icon_sites_have_icon_codes(live_client):
    df = live_client.query(
        "SELECT ID, Code FROM V_Site WHERE ID IN (?, ?, ?, ?, ?, ?) ORDER BY ID",
        [5, 13, 18, 20, 24, 25],
    )
    assert df["id"].tolist() == [5, 13, 18, 20, 24, 25]
    assert all(c.startswith("ICONTN-") for c in df["code"])


def test_bad_sql_raises_query_error(live_client):
    with pytest.raises(QueryError):
        live_client.query("SELECT NoSuchColumn FROM V_Site WHERE ID = ?", [SITE])
    # The connection is still usable after a failed statement.
    assert len(live_client.query("SELECT ID FROM V_Site WHERE ID = ?", [SITE])) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM V_Employee",
        "SELECT SSNum FROM V_Employee",
        "SELECT EmailPass FROM V_Site",
        "SELECT * FROM Prepaid_Update_Balance(1, 2)",
        "EXECUTE PROCEDURE GEN_OBJID",
        "SELECT ID FROM V_Site; DELETE FROM V_Site",
    ],
)
def test_guard_blocks_before_server(live_client, sql):
    with pytest.raises(UnsafeQueryError):
        live_client.query(sql)


def test_explicit_credentials_connect(live_client):
    """Explicit-credential construction (values taken from the same env) connects."""
    client = SiteWatchClient(
        host=os.environ["SITEWATCH_HOST"],
        database=os.environ["SITEWATCH_DATABASE"],
        user=os.environ["SITEWATCH_USER"],
        password=os.environ["SITEWATCH_PASSWORD"],
        port=int(os.environ.get("SITEWATCH_PORT") or 3050),
        fb_client=os.environ.get("SITEWATCH_FB_CLIENT") or None,
    )
    with client as c:
        df = c.query("SELECT ID FROM V_Site WHERE ID = ?", [SITE])
        assert int(df.loc[0, "id"]) == SITE
    assert client._conn is None
