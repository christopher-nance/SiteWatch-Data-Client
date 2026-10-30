"""Firebird connection management for the SiteWatch database."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import fdb
import pandas as pd

from sitewatch_data_client._config import SiteWatchConfig
from sitewatch_data_client._exceptions import ConnectionError, QueryError

logger = logging.getLogger("sitewatch_data_client")

_FETCH_CHUNK = 1000
_SQL_PREVIEW_CHARS = 100


def _sql_preview(sql: str) -> str:
    text = " ".join(sql.split())
    return text[:_SQL_PREVIEW_CHARS] + "..." if len(text) > _SQL_PREVIEW_CHARS else text


def _convert_param(value: Any) -> Any:
    """Dialect-1 ``DATE`` is a timestamp, so plain dates are sent as midnight datetimes."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    return value


def _clean_value(value: Any) -> Any:
    """Right-strip CHAR padding; decode raw bytes (charset NONE) as Windows-1252."""
    if isinstance(value, bytes):
        value = value.decode("cp1252", errors="replace")
    if isinstance(value, str):
        return value.rstrip()
    return value


def _read_only_transaction(con: fdb.Connection) -> fdb.Transaction:
    """Create the connection's reusable read-only, read-committed transaction."""
    tr = con.trans(default_tpb=fdb.ISOLATION_LEVEL_READ_COMMITED_RO)
    # fdb resolves an unfinished transaction with default_action on begin()/close().
    tr.default_action = "rollback"
    return tr


class SiteWatchConnection:
    """Thin wrapper over an ``fdb`` connection.

    Every statement runs in a read-only, read-committed transaction that is always rolled
    back, so nothing can be written even by mistake. One ``fdb`` Transaction object is
    created per connection and reused (begun and rolled back per statement), because
    ``fdb`` keeps every Transaction it creates until the connection closes.
    """

    def __init__(self, config: SiteWatchConfig) -> None:
        self._config = config
        self._connection: fdb.Connection | None = None
        self._transaction: fdb.Transaction | None = None

    @property
    def connection(self) -> fdb.Connection:
        if self._connection is None:
            raise ConnectionError(
                "Not connected. Use connect() or enter the context manager first."
            )
        return self._connection

    def connect(self) -> SiteWatchConnection:
        """Open the Firebird connection. Does nothing if it is already open.

        Raises:
            ConnectionError: If the connection fails. The message names host and port but
                never the password.
        """
        if self._connection is not None:
            return self
        cfg = self._config
        params: dict[str, Any] = {
            "dsn": cfg.dsn,
            "user": cfg.user,
            "password": cfg.password,
            "sql_dialect": cfg.sql_dialect,
            "charset": cfg.charset,
        }
        if cfg.fb_client:
            params["fb_library_name"] = cfg.fb_client
        try:
            self._connection = fdb.connect(**params)
            self._transaction = _read_only_transaction(self._connection)
            return self
        except Exception as e:
            msg = str(e)
            if cfg.password:
                msg = msg.replace(cfg.password, "***")
            raise ConnectionError(
                f"Failed to connect to SiteWatch at {cfg.host}:{cfg.port}: {msg}"
            ) from None

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        if self._connection is not None:
            try:
                if self._transaction is not None:
                    self._transaction.rollback()
                    self._transaction.close()
            except Exception:  # pragma: no cover - closing a dead transaction
                logger.debug("Error while closing read-only transaction", exc_info=True)
            finally:
                self._transaction = None
            try:
                self._connection.close()
            except Exception:  # pragma: no cover - closing a dead connection
                logger.debug("Error while closing SiteWatch connection", exc_info=True)
            finally:
                self._connection = None

    def __enter__(self) -> SiteWatchConnection:
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def query(
        self,
        sql: str,
        params: Sequence | None = None,
        *,
        max_rows: int | None = None,
    ) -> pd.DataFrame:
        """Run one statement in a read-only transaction and return a DataFrame.

        Args:
            sql: SQL text using ``?`` (qmark) placeholders.
            params: Parameter values. ``date`` values are sent as midnight ``datetime``.
            max_rows: Stop fetching after this many rows. None fetches everything. When the
                cap cuts the result short, ``df.attrs["truncated"]`` is True.

        Returns:
            Results with lower-case column names and right-stripped strings.

        Raises:
            QueryError: If execution fails. The message includes a preview of the SQL but
                never the parameters.
        """
        con = self.connection
        if self._transaction is None:
            self._transaction = _read_only_transaction(con)
        tr = self._transaction
        cur = None
        try:
            tr.begin()
            cur = tr.cursor()
            cur.execute(sql, tuple(_convert_param(p) for p in (params or ())))
            columns = [d[0].lower() for d in (cur.description or ())]
            rows: list[tuple] = []
            truncated = False
            if columns:
                while True:
                    want = _FETCH_CHUNK
                    if max_rows is not None:
                        want = min(want, max_rows + 1 - len(rows))
                    chunk = cur.fetchmany(want)
                    if not chunk:
                        break
                    rows.extend(chunk)
                    if max_rows is not None and len(rows) > max_rows:
                        rows = rows[:max_rows]
                        truncated = True
                        break
            data = [[_clean_value(v) for v in row] for row in rows]
            df = pd.DataFrame(data, columns=columns)
            df.attrs["truncated"] = truncated
            return df
        except Exception as e:
            raise QueryError(f"Query execution failed: {e}. SQL: {_sql_preview(sql)}") from e
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:  # pragma: no cover
                logger.debug("Closing cursor failed", exc_info=True)
            try:
                if tr.active:
                    tr.rollback()
            except Exception:  # pragma: no cover
                logger.debug("Rollback of read-only transaction failed", exc_info=True)
