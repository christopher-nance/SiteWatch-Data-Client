"""SiteWatchClient: main entry point for the package."""

from __future__ import annotations

import functools
import logging
from collections.abc import Sequence

import pandas as pd

from sitewatch_data_client._config import DEFAULT_PORT, SiteWatchConfig
from sitewatch_data_client._connection import SiteWatchConnection
from sitewatch_data_client._exceptions import ConfigurationError, ConnectionError
from sitewatch_data_client._safety import assert_read_only_sql, query_load_warnings
from sitewatch_data_client.resources._catalog import CatalogResource
from sitewatch_data_client.resources._labor import LaborResource
from sitewatch_data_client.resources._memberships import MembershipsResource
from sitewatch_data_client.resources._sales import SalesResource
from sitewatch_data_client.resources._sites import SitesResource

logger = logging.getLogger("sitewatch_data_client")

DEFAULT_MAX_ROWS = 10000


class SiteWatchClient:
    """Read-only client for the DRB SiteWatch POS database (Firebird 2.5, dialect 1).

    Every statement runs in its own read-only transaction. Typed methods default to the
    ICON car-wash sites (5, 13, 18, 20, 24, 25) and require a bounded date range.

    Args:
        host: Query server host name or IP. If host, database, user and password are all
            None, configuration is loaded from ``SITEWATCH_*`` environment variables.
        database: Server-side database path.
        user: Firebird user (normally ``REPORTS``).
        password: Firebird password.
        port: Firebird port. Defaults to 3050, or ``SITEWATCH_PORT`` in environment mode.
            An explicit value always wins over the environment.
        fb_client: Absolute path to the Firebird 2.5 ``fbclient.dll``.

    Raises:
        ConfigurationError: If explicit credentials are incomplete, or environment
            configuration is missing.

    Examples:
        # From environment variables (load .env first with python-dotenv)
        from dotenv import load_dotenv
        load_dotenv()

        with SiteWatchClient() as client:
            df = client.query("SELECT ID, Code, SiteName FROM V_Site WHERE ID = ?", [24])

        # Explicit credentials
        client = SiteWatchClient(host="...", database="...", user="REPORTS",
                                 password="...", fb_client="C:/path/to/fbclient.dll")
        client.connect()
        ...
        client.close()
    """

    def __init__(
        self,
        host: str | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
        port: int | None = None,
        fb_client: str | None = None,
    ) -> None:
        if all(v is None for v in (host, database, user, password)):
            self._config = SiteWatchConfig.load_config()
            if fb_client is not None:
                self._config.fb_client = fb_client
            if port is not None:
                self._config.port = port
        else:
            if not all((host, database, user, password)):
                raise ConfigurationError(
                    "When providing explicit credentials, host, database, user and "
                    "password are all required."
                )
            self._config = SiteWatchConfig(
                host=host,  # type: ignore[arg-type]
                database=database,  # type: ignore[arg-type]
                user=user,  # type: ignore[arg-type]
                password=password,  # type: ignore[arg-type]
                port=DEFAULT_PORT if port is None else port,
                fb_client=fb_client,
            )

        self._conn: SiteWatchConnection | None = None

    @functools.cached_property
    def sites(self) -> SitesResource:
        """Site listing (IDs, codes, addresses) and data-freshness methods."""
        return SitesResource(self)

    @functools.cached_property
    def sales(self) -> SalesResource:
        """Daily and hourly sales, car counts, item mix, tenders and sale headers."""
        return SalesResource(self)

    @functools.cached_property
    def catalog(self) -> CatalogResource:
        """Item, report-category and terminal reference data."""
        return CatalogResource(self)

    @functools.cached_property
    def memberships(self) -> MembershipsResource:
        """ARM monthly-pass plans, activity, new members and member status."""
        return MembershipsResource(self)

    @functools.cached_property
    def labor(self) -> LaborResource:
        """Employees, payroll timecards, labor statistics and productivity."""
        return LaborResource(self)

    def connect(self) -> SiteWatchClient:
        """Open the Firebird connection.

        Calling it again while connected does nothing, so a second ``connect()`` (or a
        ``with`` block around a connected client) never leaks a server attachment.

        Raises:
            ConnectionError: If the connection fails.
        """
        if self._conn is not None:
            return self
        self._conn = SiteWatchConnection(self._config)
        self._conn.connect()
        return self

    def close(self) -> None:
        """Close the Firebird connection. Safe to call more than once."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SiteWatchClient:
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _execute(
        self, sql: str, params: Sequence | None = None, *, max_rows: int | None = None
    ) -> pd.DataFrame:
        """Internal: run trusted, library-owned SQL on the active connection."""
        if self._conn is None:
            raise ConnectionError("Not connected. Use connect() or a context manager.")
        return self._conn.query(sql, params, max_rows=max_rows)

    def query(
        self,
        sql: str,
        params: Sequence | None = None,
        *,
        max_rows: int | None = DEFAULT_MAX_ROWS,
    ) -> pd.DataFrame:
        """Execute a raw, read-only ``SELECT`` and return a pandas DataFrame.

        Power-user escape hatch for ad-hoc queries. The SQL is checked by a lexical guard
        (``SELECT``/``WITH`` only, one statement, no DML/DDL, no data-changing procedures,
        no employee secrets, ``V_Site`` credentials or card data) and then runs in a
        read-only transaction.

        This is a live production database shared by about 60 sites. Keep queries light:

        * Bound history views (``V_Sale``, ``V_SaleItems``, ``V_SalePasses``,
          ``V_PunchInterval`` ...) by ``Site`` **and** a date range.
        * Use Firebird ``SELECT FIRST n``; ``LIMIT`` and ``TOP`` do not exist.
        * Avoid ``IN (SELECT ...)``; use a derived table plus ``JOIN``.
        * Pass every value as a ``?`` parameter rather than formatting it into the SQL.
        * ``max_rows`` limits only the rows fetched, not the work the server does: an
          aggregate or an unbounded scan still reads all history. A warning is logged when
          a history view is read without a ``WHERE`` clause, or when a procedure known to
          run for minutes (``Arm_Monthly_Current_Status``, ``Arm_Summary_For_Date_Range``,
          ``CustClubPlan_Balances``) is called. There is no statement timeout.

        Args:
            sql: SQL text with ``?`` placeholders.
            params: Parameter values. ``date`` values are sent as midnight timestamps.
            max_rows: Maximum rows to fetch (default 10,000). None removes the cap. A
                warning is logged when the cap truncates the result.

        Returns:
            Results with lower-case column names and trimmed strings.

        Raises:
            UnsafeQueryError: If the guard rejects the SQL.
            ConnectionError: If the client is not connected.
            QueryError: If Firebird rejects or fails the query.

        Examples:
            >>> df = client.query("SELECT ID, Code FROM V_Site WHERE ID = ?", [24])
            >>> df = client.query(
            ...     "SELECT FIRST 10 ObjID, LogDate, Total FROM V_Sale "
            ...     "WHERE Site = ? AND LogDate >= ? AND LogDate < ?",
            ...     [24, date(2026, 10, 14), date(2026, 10, 15)],
            ... )
        """
        assert_read_only_sql(sql)
        for warning in query_load_warnings(sql):
            logger.warning(warning)
        df = self._execute(sql, params, max_rows=max_rows)
        if df.attrs.get("truncated"):
            logger.warning(
                "query() result truncated at max_rows=%s; pass max_rows=None or narrow the "
                "query to fetch everything.",
                max_rows,
            )
        return df
