"""Tests for SiteWatchClient and SiteWatchConnection (no database)."""

import logging
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

import sitewatch_data_client
from sitewatch_data_client import SiteWatchClient
from sitewatch_data_client._config import SiteWatchConfig
from sitewatch_data_client._connection import SiteWatchConnection
from sitewatch_data_client._exceptions import (
    ConfigurationError,
    ConnectionError,
    QueryError,
    UnsafeQueryError,
)
from tests.conftest import make_df

FAKE_PASSWORD = "not-a-real-password"


def _config(**overrides) -> SiteWatchConfig:
    values = dict(
        host="example-host", database="Example.FDB", user="REPORTS", password=FAKE_PASSWORD
    )
    values.update(overrides)
    return SiteWatchConfig(**values)


class TestSiteWatchClient:
    def test_version(self):
        assert sitewatch_data_client.__version__ == "0.1.0"

    def test_explicit_credentials(self):
        client = SiteWatchClient(
            host="h",
            database="d",
            user="u",
            password="p",
            port=3051,
            fb_client="C:/fb/fbclient.dll",
        )
        assert client._config.port == 3051
        assert client._config.fb_client == "C:/fb/fbclient.dll"
        assert client._config.dsn == "h/3051:d"

    def test_partial_credentials_raises(self):
        with pytest.raises(ConfigurationError, match="all required"):
            SiteWatchClient(host="h", user="u")

    def test_env_credentials(self):
        with patch("sitewatch_data_client._config.SiteWatchConfig.load_config") as mock:
            mock.return_value = MagicMock()
            SiteWatchClient()
            mock.assert_called_once()

    def test_execute_without_connect_raises(self):
        with patch("sitewatch_data_client._config.SiteWatchConfig.load_config") as mock:
            mock.return_value = MagicMock()
            client = SiteWatchClient()
            with pytest.raises(ConnectionError, match="Not connected"):
                client._execute("SELECT 1 FROM RDB$DATABASE")

    def test_query_passes_sql_and_params(self, mock_client):
        mock_client._conn.query.return_value = make_df({"id": [24], "code": ["ICONTN-000"]})
        df = mock_client.query("SELECT ID, Code FROM V_Site WHERE ID = ?", [24])
        sql, params = mock_client._conn.query.call_args.args
        assert sql == "SELECT ID, Code FROM V_Site WHERE ID = ?"
        assert params == [24]
        assert mock_client._conn.query.call_args.kwargs["max_rows"] == 10000
        assert list(df.columns) == ["id", "code"]

    def test_query_guard_runs_before_execute(self, mock_client):
        with pytest.raises(UnsafeQueryError):
            mock_client.query("DELETE FROM V_Sale")
        mock_client._conn.query.assert_not_called()

    def test_query_truncation_warns(self, mock_client, caplog):
        df = make_df({"a": [1, 2]})
        df.attrs["truncated"] = True
        mock_client._conn.query.return_value = df
        with caplog.at_level(logging.WARNING, logger="sitewatch_data_client"):
            mock_client.query("SELECT a FROM t", max_rows=2)
        assert "truncated" in caplog.text

    def test_close_idempotent(self, mock_client):
        conn = mock_client._conn
        mock_client.close()
        mock_client.close()
        conn.close.assert_called_once()
        assert mock_client._conn is None

    def test_context_manager(self):
        client = SiteWatchClient(host="h", database="d", user="u", password="p")
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            with client as c:
                assert c is client
                assert client._conn is not None
            fdb_mock.connect.return_value.close.assert_called_once()
        assert client._conn is None


class TestSiteWatchConnection:
    def test_connect_params(self):
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            SiteWatchConnection(_config(fb_client="C:/fb/fbclient.dll")).connect()
        kwargs = fdb_mock.connect.call_args.kwargs
        assert kwargs["dsn"] == "example-host/3050:Example.FDB"
        assert kwargs["sql_dialect"] == 1
        assert kwargs["charset"] == "NONE"
        assert kwargs["fb_library_name"] == "C:/fb/fbclient.dll"

    def test_connect_without_fb_client(self):
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            SiteWatchConnection(_config()).connect()
        assert "fb_library_name" not in fdb_mock.connect.call_args.kwargs

    def test_connect_error_never_leaks_password(self):
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            fdb_mock.connect.side_effect = RuntimeError(f"login failed for {FAKE_PASSWORD}")
            with pytest.raises(ConnectionError) as exc:
                SiteWatchConnection(_config()).connect()
        msg = str(exc.value)
        assert FAKE_PASSWORD not in msg
        assert "example-host:3050" in msg
        assert exc.value.__cause__ is None

    def _connected(self, rows, description):
        conn = SiteWatchConnection(_config())
        fake = MagicMock()
        tr = fake.trans.return_value
        cur = tr.cursor.return_value
        cur.description = description
        data = list(rows)

        def fetchmany(n):
            out = data[:n]
            del data[:n]
            return out

        cur.fetchmany.side_effect = fetchmany
        conn._connection = fake
        return conn, fake, tr, cur

    def test_query_read_only_and_cleans(self):
        conn, fake, tr, cur = self._connected(
            [(24, "ICONTN-000   ", b"Caf\xe9  "), (5, None, "x")],
            [("ID",), ("CODE",), ("NAME",)],
        )
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            df = conn.query(
                "SELECT ID, Code, Name FROM V_Site WHERE ID = ?", [24, date(2026, 10, 14)]
            )
            assert fake.trans.call_args.kwargs["default_tpb"] is (
                fdb_mock.ISOLATION_LEVEL_READ_COMMITED_RO
            )
        assert list(df.columns) == ["id", "code", "name"]
        assert df.loc[0, "code"] == "ICONTN-000"
        assert df.loc[0, "name"] == "Caf\u00e9"
        sent = cur.execute.call_args.args[1]
        assert sent == (24, datetime(2026, 10, 14))
        tr.rollback.assert_called_once()
        assert df.attrs["truncated"] is False

    def test_query_max_rows_truncates(self):
        conn, _, tr, _ = self._connected([(i,) for i in range(5)], [("A",)])
        with patch("sitewatch_data_client._connection.fdb"):
            df = conn.query("SELECT a FROM t", max_rows=3)
        assert len(df) == 3
        assert df.attrs["truncated"] is True
        tr.rollback.assert_called_once()

    def test_query_max_rows_exact_not_truncated(self):
        conn, _, _, _ = self._connected([(i,) for i in range(3)], [("A",)])
        with patch("sitewatch_data_client._connection.fdb"):
            df = conn.query("SELECT a FROM t", max_rows=3)
        assert len(df) == 3
        assert df.attrs["truncated"] is False

    def test_query_error_wrapped_with_preview_not_params(self):
        conn, _, tr, cur = self._connected([], [("A",)])
        cur.execute.side_effect = RuntimeError("boom")
        with patch("sitewatch_data_client._connection.fdb"):
            with pytest.raises(QueryError) as exc:
                conn.query("SELECT a FROM t WHERE x = ?", ["secret-param-value"])
        assert "SELECT a FROM t" in str(exc.value)
        assert "secret-param-value" not in str(exc.value)
        tr.rollback.assert_called_once()

    def test_query_not_connected(self):
        with pytest.raises(ConnectionError):
            SiteWatchConnection(_config()).query("SELECT 1 FROM RDB$DATABASE")
