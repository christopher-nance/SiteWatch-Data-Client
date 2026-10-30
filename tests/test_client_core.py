"""Contract tests for SiteWatchClient: configuration, lifecycle and the ``query()`` guard.

Complements ``test_client.py`` (connection internals) and ``test_safety.py`` (guard rules in
isolation) by checking the behaviour through the public client surface. No database; every
value is made up.
"""

import logging
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from sitewatch_data_client import SiteWatchClient
from sitewatch_data_client._client import DEFAULT_MAX_ROWS
from sitewatch_data_client._config import SiteWatchConfig
from sitewatch_data_client._exceptions import (
    ConfigurationError,
    ConnectionError,
    QueryError,
    SiteWatchError,
    UnsafeQueryError,
    ValidationError,
)
from sitewatch_data_client._safety import BLOCKED_PROCEDURES
from tests.conftest import make_df

FAKE_PASSWORD = "made-up-password-123"

ENV = {
    "SITEWATCH_HOST": "example-host",
    "SITEWATCH_DATABASE": "E:\\Example\\SiteWatch.FDB",
    "SITEWATCH_USER": "REPORTS",
    "SITEWATCH_PASSWORD": FAKE_PASSWORD,
}


def _sent(mock_client):
    """Return (sql, params, kwargs) of the last statement sent to the mocked connection."""
    call = mock_client._conn.query.call_args
    sql, params = call.args
    return sql, params, call.kwargs


# --- __init__ ---------------------------------------------------------------------------


class TestInit:
    def test_env_config_loaded(self, monkeypatch):
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("SITEWATCH_PORT", raising=False)
        monkeypatch.delenv("SITEWATCH_FB_CLIENT", raising=False)
        client = SiteWatchClient()
        cfg = client._config
        assert cfg.host == "example-host"
        assert cfg.port == 3050
        assert cfg.fb_client is None
        assert cfg.sql_dialect == 1
        assert cfg.charset == "NONE"
        assert client._conn is None

    def test_env_config_with_fb_client_and_port_override(self, monkeypatch):
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SITEWATCH_PORT", "3050")
        client = SiteWatchClient(port=3055, fb_client="C:/fb/fbclient.dll")
        assert client._config.port == 3055
        assert client._config.fb_client == "C:/fb/fbclient.dll"
        assert client._config.dsn == "example-host/3055:E:\\Example\\SiteWatch.FDB"

    def test_explicit_default_port_overrides_env(self, monkeypatch):
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SITEWATCH_PORT", "3055")
        assert SiteWatchClient()._config.port == 3055
        assert SiteWatchClient(port=3050)._config.port == 3050

    def test_env_missing_raises_configuration_error(self, monkeypatch):
        for k in ENV:
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(ConfigurationError, match="SITEWATCH_HOST"):
            SiteWatchClient()

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(host="h"),
            dict(host="h", database="d", user="u"),
            dict(database="d", user="u", password="p"),
            dict(host="h", database="d", user="u", password=""),
            dict(host="", database="d", user="u", password="p"),
        ],
    )
    def test_incomplete_explicit_credentials(self, kwargs):
        with pytest.raises(ConfigurationError, match="all required"):
            SiteWatchClient(**kwargs)

    def test_explicit_credentials_ignore_env(self, monkeypatch):
        for k in ENV:
            monkeypatch.delenv(k, raising=False)
        client = SiteWatchClient(host="h", database="d", user="u", password=FAKE_PASSWORD)
        assert client._config.dsn == "h/3050:d"
        assert client._config.fb_client is None

    def test_password_not_in_repr(self):
        client = SiteWatchClient(host="h", database="d", user="u", password=FAKE_PASSWORD)
        assert FAKE_PASSWORD not in repr(client._config)

    def test_exception_hierarchy(self):
        assert issubclass(ConfigurationError, SiteWatchError)
        assert issubclass(ConnectionError, SiteWatchError)
        assert issubclass(QueryError, SiteWatchError)
        assert issubclass(UnsafeQueryError, ValidationError)


# --- connect / close / context manager --------------------------------------------------


class TestLifecycle:
    def _client(self):
        return SiteWatchClient(host="h", database="d", user="u", password=FAKE_PASSWORD)

    def test_connect_returns_self_and_uses_dialect_1(self):
        client = self._client()
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            assert client.connect() is client
            kwargs = fdb_mock.connect.call_args.kwargs
        assert kwargs["sql_dialect"] == 1
        assert kwargs["charset"] == "NONE"
        assert kwargs["dsn"] == "h/3050:d"
        client.close()
        assert client._conn is None

    def test_connect_failure_raises_connection_error_without_password(self):
        client = self._client()
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            fdb_mock.connect.side_effect = RuntimeError(f"bad login {FAKE_PASSWORD}")
            with pytest.raises(ConnectionError) as exc:
                client.connect()
        assert FAKE_PASSWORD not in str(exc.value)

    def test_query_after_failed_connect_raises_connection_error(self):
        client = self._client()
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            fdb_mock.connect.side_effect = RuntimeError("unreachable")
            with pytest.raises(ConnectionError):
                client.connect()
        with pytest.raises(ConnectionError, match="Not connected"):
            client.query("SELECT ID FROM V_Site WHERE ID = ?", [24])

    def test_context_manager_closes_on_error(self):
        client = self._client()
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            with pytest.raises(RuntimeError):
                with client:
                    raise RuntimeError("caller error")
            fdb_mock.connect.return_value.close.assert_called_once()
        assert client._conn is None

    def test_close_without_connect_is_safe(self):
        client = self._client()
        client.close()
        client.close()
        assert client._conn is None

    def test_query_before_connect(self):
        with pytest.raises(ConnectionError, match="Not connected"):
            self._client().query("SELECT ID FROM V_Site")

    def test_each_query_uses_read_only_transaction_and_rolls_back(self):
        client = self._client()
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            client.connect()
            fake = fdb_mock.connect.return_value
            tr = fake.trans.return_value
            tr.cursor.return_value.description = [("ID",)]
            tr.cursor.return_value.fetchmany.side_effect = [[(24,)], [], [(5,)], []]
            client.query("SELECT ID FROM V_Site WHERE ID = ?", [24])
            client.query("SELECT ID FROM V_Site WHERE ID = ?", [5])
            # One reusable Transaction per connection (fdb never forgets Transactions),
            # begun and rolled back per statement; every cursor is closed.
            fake.trans.assert_called_once()
            assert (
                fake.trans.call_args.kwargs["default_tpb"]
                is fdb_mock.ISOLATION_LEVEL_READ_COMMITED_RO
            )
            assert tr.default_action == "rollback"
            assert tr.begin.call_count == 2
            assert tr.rollback.call_count == 2
            assert tr.cursor.return_value.close.call_count == 2
            tr.commit.assert_not_called()
        client.close()
        tr.close.assert_called_once()

    def test_transaction_rolled_back_and_cursor_closed_on_error(self):
        client = self._client()
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            client.connect()
            tr = fdb_mock.connect.return_value.trans.return_value
            tr.cursor.return_value.execute.side_effect = RuntimeError("boom")
            with pytest.raises(QueryError):
                client.query("SELECT ID FROM V_Site WHERE ID = ?", [24])
            tr.rollback.assert_called_once()
            tr.cursor.return_value.close.assert_called_once()
        client.close()

    def test_second_connect_reuses_open_connection(self):
        client = self._client()
        with patch("sitewatch_data_client._connection.fdb") as fdb_mock:
            client.connect()
            first = client._conn
            assert client.connect() is client
            with client:
                assert client._conn is first
            fdb_mock.connect.assert_called_once()
            fdb_mock.connect.return_value.close.assert_called_once()
        assert client._conn is None


# --- query() ----------------------------------------------------------------------------


class TestQuery:
    def test_returns_dataframe_rows(self, mock_client):
        mock_client._conn.query.return_value = make_df(
            {"id": [5, 24], "code": ["ICONTN-001", "ICONTN-002"]}
        )
        df = mock_client.query("SELECT ID, Code FROM V_Site WHERE ID IN (?, ?)", [5, 24])
        assert df.to_dict("records") == [
            {"id": 5, "code": "ICONTN-001"},
            {"id": 24, "code": "ICONTN-002"},
        ]

    def test_empty_result(self, mock_client):
        mock_client._conn.query.return_value = make_df({"id": [], "code": []})
        df = mock_client.query("SELECT ID, Code FROM V_Site WHERE ID = ?", [-1])
        assert df.empty
        assert list(df.columns) == ["id", "code"]

    def test_params_are_sent_separately_not_interpolated(self, mock_client):
        sql_in = (
            "SELECT FIRST 10 ObjID, Total FROM V_Sale "
            "WHERE Site = ? AND LogDate >= ? AND LogDate < ?"
        )
        params_in = [24, date(2026, 10, 5), date(2026, 10, 12)]
        mock_client.query(sql_in, params_in)
        sql, params, kwargs = _sent(mock_client)
        assert sql == sql_in
        assert params == params_in
        assert "24" not in sql and "2026" not in sql
        assert kwargs == {"max_rows": DEFAULT_MAX_ROWS}

    def test_injection_text_in_param_is_not_inspected(self, mock_client):
        # A hostile *value* is harmless because it is bound, so the guard must not reject it.
        mock_client.query("SELECT ID FROM V_Site WHERE Code = ?", ["x'; DELETE FROM V_Sale --"])
        _, params, _ = _sent(mock_client)
        assert params == ["x'; DELETE FROM V_Sale --"]

    def test_default_max_rows_is_10000(self, mock_client):
        assert DEFAULT_MAX_ROWS == 10000
        mock_client.query("SELECT ID FROM V_Site")
        assert _sent(mock_client)[2]["max_rows"] == 10000

    @pytest.mark.parametrize("cap", [None, 1, 500])
    def test_max_rows_forwarded(self, mock_client, cap):
        mock_client.query("SELECT ID FROM V_Site", max_rows=cap)
        assert _sent(mock_client)[2]["max_rows"] == cap

    def test_no_warning_when_not_truncated(self, mock_client, caplog):
        df = make_df({"id": [1]})
        df.attrs["truncated"] = False
        mock_client._conn.query.return_value = df
        with caplog.at_level(logging.WARNING, logger="sitewatch_data_client"):
            mock_client.query("SELECT ID FROM V_Site")
        assert "truncated" not in caplog.text

    def test_warning_when_truncated_names_cap(self, mock_client, caplog):
        df = make_df({"id": [1, 2, 3]})
        df.attrs["truncated"] = True
        mock_client._conn.query.return_value = df
        with caplog.at_level(logging.WARNING, logger="sitewatch_data_client"):
            out = mock_client.query("SELECT ID FROM V_Site", max_rows=3)
        assert "max_rows=3" in caplog.text
        assert len(out) == 3

    def test_query_error_propagates(self, mock_client):
        mock_client._conn.query.side_effect = QueryError("Query execution failed: boom")
        with pytest.raises(QueryError):
            mock_client.query("SELECT NoSuchColumn FROM V_Site")

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT ID, Code FROM V_Site WHERE ID = ?",
            "  select id from v_site",
            "-- comment\nSELECT ID FROM V_Site;",
            "/* c */ WITH s AS (SELECT ID FROM V_Site) SELECT ID FROM s",
            "SELECT FIRST 5 SKIP 5 ObjID FROM V_Sale WHERE Site = ?",
            "SELECT * FROM V_Item WHERE ObjID = ?",
            "SELECT * FROM Labor_Statistics(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            "SELECT ID FROM V_Site WHERE Code = 'UPDATE; DELETE'",
        ],
    )
    def test_allowed(self, mock_client, sql):
        mock_client.query(sql)
        mock_client._conn.query.assert_called_once()

    @pytest.mark.parametrize(
        "sql, reason",
        [
            ("", "non-empty"),
            ("-- only a comment", "only comments"),
            ("DELETE FROM V_Sale", "Only SELECT or WITH"),
            ("UPDATE V_Site SET Code = 'x'", "Only SELECT or WITH"),
            ("EXECUTE PROCEDURE GEN_OBJID", "Only SELECT or WITH"),
            ("SELECT ID FROM V_Site; DROP TABLE x", "Only one statement"),
            ("SELECT ID INTO :x FROM V_Site", "INTO"),
            ("WITH d AS (DELETE FROM t) SELECT 1 FROM d", "DELETE"),
            ("SELECT * FROM GEN_OBJID", "GEN_OBJID"),
            ("SELECT * FROM Prepaid_Update_Balance(1, 2)", "PREPAID_UPDATE_BALANCE"),
            ("SELECT * FROM Something_Update_Totals(1)", "data-changing"),
            ("SELECT * FROM Foo_Set_Bar(1)", "data-changing"),
            ("SELECT f_GetDecryptedSSN(x) FROM V_Employee", "F_GETDECRYPTEDSSN"),
            ("SELECT SSNum FROM V_Employee", "SSNUM"),
            ("SELECT PWord FROM V_Employee", "PWORD"),
            ("SELECT SecurityCode FROM V_Employee", "SECURITYCODE"),
            ("SELECT DrvLNum FROM V_Employee", "DRVLNUM"),
            ("SELECT * FROM V_Employee", "SELECT \\*"),
            ("SELECT e.* FROM V_Employee e", "SELECT \\*"),
            ("SELECT FIRST 1 * FROM V_Site", "SELECT \\*"),
            ("SELECT EmailPass FROM V_Site", "EMAILPASS"),
            ("SELECT MaskedPAN FROM V_SalePayments", "MASKEDPAN"),
        ],
    )
    def test_rejected_before_execute(self, mock_client, sql, reason):
        with pytest.raises(UnsafeQueryError, match=reason):
            mock_client.query(sql)
        mock_client._conn.query.assert_not_called()

    @pytest.mark.parametrize("proc", sorted(BLOCKED_PROCEDURES))
    def test_all_15_blocked_procedures(self, mock_client, proc):
        with pytest.raises(UnsafeQueryError):
            mock_client.query(f"SELECT * FROM {proc.lower()}(?)", [1])
        mock_client._conn.query.assert_not_called()

    def test_blocked_procedure_count(self):
        assert len(BLOCKED_PROCEDURES) == 15

    def test_unsafe_query_is_catchable_as_validation_error(self, mock_client):
        with pytest.raises(ValidationError):
            mock_client.query("INSERT INTO t VALUES (1)")

    def test_execute_bypasses_guard_for_library_sql(self, mock_client):
        # _execute is for trusted, library-owned SQL; the guard runs only in query().
        mock_client._execute("SELECT 1 FROM RDB$DATABASE", ())
        mock_client._conn.query.assert_called_once()
        assert _sent(mock_client)[2] == {"max_rows": None}


def test_config_load_config_is_patched_by_fixture(mock_client):
    assert isinstance(mock_client._config, MagicMock)
    assert not isinstance(mock_client._config, SiteWatchConfig)
