"""Sites resource: site master data and data-freshness probes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from sitewatch_data_client._constants import (
    ICON_OFFICE_SITE_ID,
    ICON_QUERY_SERVER_SITE_ID,
)
from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client._filters import (
    business_today,
    normalize_site,
    normalize_sites,
    placeholders,
)
from sitewatch_data_client.types._sites import Site, SiteFreshness

if TYPE_CHECKING:
    from sitewatch_data_client._client import SiteWatchClient

#: How far back the freshness probes look. Bounding ``MAX()`` by a floor keeps each probe on
#: the (Site, LogDate) index instead of scanning all history.
FRESHNESS_FLOOR_DAYS = 400


# --- SQL builders (library-owned text; every value is a ``?`` parameter) ----------------------


def sites_sql(site_ids: list[int]) -> tuple[str, list[Any]]:
    """Site master rows: ``V_Site`` joined 1:1 to ``V_System`` (59/59, verified).

    Never ``SELECT *`` on ``V_Site``: it holds email, SMTP and processor credentials.
    """
    sql = f"""
        SELECT st.ID, st.Code, st.SiteName, st.Enabled,
               y.Address, y.Address2, y.City, y.State, y.Zip, y.Phone
        FROM V_Site st
        LEFT JOIN V_System y ON y.Site = st.ID
        WHERE st.ID IN ({placeholders(len(site_ids))})
        ORDER BY st.ID
    """.strip()
    return sql, list(site_ids)


def latest_sale_date_sql(site_id: int, floor: datetime) -> tuple[str, list[Any]]:
    """Newest sale business date for one site, bounded below by ``floor``."""
    sql = "SELECT MAX(s.LogDate) AS LatestDate FROM V_Sale s WHERE s.Site = ? AND s.LogDate >= ?"
    return sql, [site_id, floor]


def latest_punch_date_sql(site_id: int, floor: datetime) -> tuple[str, list[Any]]:
    """Newest punch date for one site, bounded below by ``floor``."""
    sql = (
        "SELECT MAX(p.PunchDate) AS LatestDate FROM V_PunchInterval p "
        "WHERE p.Site = ? AND p.PunchDate >= ?"
    )
    return sql, [site_id, floor]


def replication_sql(site_ids: list[int]) -> tuple[str, list[Any]]:
    """Last replication packet applied per site (``V_Replication`` is 1 row per site)."""
    sql = (
        "SELECT r.Site, r.PacketAppliedDate FROM V_Replication r "
        f"WHERE r.Site IN ({placeholders(len(site_ids))}) ORDER BY r.Site"
    )
    return sql, list(site_ids)


# --- Row helpers ---------------------------------------------------------------------------------


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _text(value: Any) -> str | None:
    """Trim text; None, NaN and empty strings become None."""
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _to_datetime(value: Any) -> datetime | None:
    if _is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return pd.Timestamp(value).to_pydatetime()


def _to_date(value: Any) -> date | None:
    dt = _to_datetime(value)
    return dt.date() if dt is not None else None


def _row_to_site(r: dict[str, Any]) -> Site:
    return Site(
        site_id=int(r["id"]),
        code=_text(r["code"]) or "",
        name=_text(r["sitename"]) or "",
        enabled=not _is_missing(r["enabled"]) and int(r["enabled"]) == 1,
        address=_text(r.get("address")),
        address2=_text(r.get("address2")),
        city=_text(r.get("city")),
        state=_text(r.get("state")),
        zip=_text(r.get("zip")),
        phone=_text(r.get("phone")),
    )


def _today() -> date:
    """Today's US Central business date. Isolated so tests can pin the freshness floor."""
    return business_today()


class SitesResource:
    """Site listing and data-freshness methods.

    Access via ``client.sites``.
    """

    def __init__(self, client: SiteWatchClient) -> None:
        self._client = client

    def list(
        self,
        sites: int | Iterable[int] | None = None,
        *,
        include_office: bool = False,
    ) -> list[Site]:
        """List sites with their code, name, enabled flag, address and phone.

        Identity (code, name, enabled) comes from ``V_Site``; address and phone come from
        ``V_System``, joined 1:1 on ``V_System.Site = V_Site.ID``. ``V_System.Name`` is
        always ``"System"`` and is not used. Only an explicit column list is read from
        ``V_Site``, because that view also holds credentials.

        One small statement against two 59-row views; it is safe to call freely.

        Args:
            sites: A site ID, an iterable of site IDs, or None for the ICON car-wash sites
                (5, 13, 18, 20, 24, 25). Explicit IDs are returned exactly as asked, which can
                include legacy ``OLD-*`` sites. IDs that do not exist are simply absent from
                the result.
            include_office: Also include the ICON office (99) and query server (100), which
                have no sales or punches.

        Returns:
            Sites ordered by site ID.

        Raises:
            ValidationError: If a site ID is not a positive int, or ``sites`` is empty.
            ConnectionError: If the client is not connected.
            QueryError: If the query fails.

        Examples:
            >>> with SiteWatchClient() as client:
            ...     for site in client.sites.list():
            ...         print(site.site_id, site.code, site.city)
            >>> client.sites.list(include_office=True)   # adds 99 and 100
            >>> client.sites.list([5, 24])
        """
        site_ids = normalize_sites(sites)
        if include_office:
            site_ids = sorted({*site_ids, ICON_OFFICE_SITE_ID, ICON_QUERY_SERVER_SITE_ID})
        sql, params = sites_sql(site_ids)
        df = self._client._execute(sql, params)
        return [_row_to_site(r) for r in df.to_dict("records")]

    def get(self, site_id: int) -> Site:
        """Get one site by ID.

        Args:
            site_id: The SiteWatch site ID (``V_Site.ID``), for example 24. This is not the
                number in the site code: site 13 is ``ICONTN-015``.

        Returns:
            The site.

        Raises:
            ValidationError: If ``site_id`` is not a positive int or no such site exists.
            ConnectionError: If the client is not connected.
            QueryError: If the query fails.

        Examples:
            >>> site = client.sites.get(24)
            >>> site.code
            'ICONTN-030'
        """
        site_id = normalize_site(site_id)
        found = self.list([site_id])
        if not found:
            raise ValidationError(f"Unknown site ID: {site_id}.")
        return found[0]

    def data_freshness(self, sites: int | Iterable[int] | None = None) -> list[SiteFreshness]:
        """Report how current the central database is for each site.

        The central database lags the sites (at the time of writing it ended around
        2026-10-15 for sales and 2026-10-16 for punches). Call this before trusting a recent
        date window: an empty result for "last week" usually means the data has not arrived,
        not that the query is wrong.

        For each site, one at a time, it runs two cheap probes, ``MAX(V_Sale.LogDate)`` and
        ``MAX(V_PunchInterval.PunchDate)``, each bounded below by a floor of today minus 400
        days (computed in Python and sent as a parameter). It then reads
        ``V_Replication.PacketAppliedDate`` for all requested sites in one statement. A
        ``FIRST 1 ... ORDER BY LogDate DESC`` probe was measured at about 20 s per site and is
        deliberately not used; the bounded ``MAX`` takes about 1 s.

        Statements issued: 1 (site codes) + 2 per site + 1 (replication). For the six ICON
        sites that is 14 small statements, run sequentially: about 6.6 s measured, up to
        about 20 s on a cold server cache.

        Replication: at ICON only the office hub (site 99) carries packet dates in
        ``V_Replication``; the car-wash sites' rows hold NULL, so their
        ``replication_applied_at`` is None. Pass ``[99]`` (or include it) to see when the
        last packet reached the central database.

        Args:
            sites: A site ID, an iterable of site IDs, or None for the ICON car-wash sites.

        Returns:
            One entry per requested site, ordered by site ID. A site with nothing inside the
            400-day floor reports None for that date; a site whose ``V_Replication`` row is
            missing or NULL reports ``replication_applied_at=None``.

        Raises:
            ValidationError: If a site ID is invalid, ``sites`` is empty, or a requested
                site does not exist.
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> for f in client.sites.data_freshness():
            ...     print(f.site_code, f.latest_sale_date, f.latest_punch_date)
            >>> client.sites.data_freshness(24)
        """
        site_ids = normalize_sites(sites)
        codes = {s.site_id: s.code for s in self.list(site_ids)}
        missing = [sid for sid in site_ids if sid not in codes]
        if missing:
            raise ValidationError(f"Unknown site ID(s): {', '.join(map(str, missing))}.")

        today = _today()
        floor_day = today - timedelta(days=FRESHNESS_FLOOR_DAYS)
        floor = datetime(floor_day.year, floor_day.month, floor_day.day)

        latest: dict[int, tuple[date | None, date | None]] = {}
        for sid in site_ids:
            sale_df = self._client._execute(*latest_sale_date_sql(sid, floor))
            punch_df = self._client._execute(*latest_punch_date_sql(sid, floor))
            latest[sid] = (_first_date(sale_df), _first_date(punch_df))

        rep_df = self._client._execute(*replication_sql(site_ids))
        replication: dict[int, datetime | None] = {}
        for r in rep_df.to_dict("records"):
            replication[int(r["site"])] = _to_datetime(r["packetapplieddate"])

        return [
            SiteFreshness(
                site_id=sid,
                site_code=codes[sid],
                latest_sale_date=latest[sid][0],
                latest_punch_date=latest[sid][1],
                replication_applied_at=replication.get(sid),
            )
            for sid in site_ids
        ]


def _first_date(df: pd.DataFrame) -> date | None:
    if df.empty:
        return None
    return _to_date(df.iloc[0, 0])
