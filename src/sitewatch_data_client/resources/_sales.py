"""Sales resource: daily and hourly sales, car counts, item mix, tenders and sale detail.

Business rules (verified against live ICON data, see the design spec section 7.0):

* A **completed** sale is ``f_IsCompleted(Status) = 1`` (``C:P`` and ``C:PW``). ``Notes``
  rows (ARM declines and expiries) carry a nonzero ``Total`` and are never counted.
* ``I:P-W`` sales are paid but the wash is still pending. SiteWatch's car count includes
  them; ``sales_total`` and ``revenue`` do not. ``daily_summary`` reports them separately.
* The business date is ``LogDate`` (a midnight timestamp), filtered half-open.
* Car count is the ``TotalCarCount`` sale fact in profit center 1, looked up by name.
* Tenders are base type 180 lines (``Amt`` negative for money received), named through
  ``V_Item`` and ``V_ItemRptCategory``, never ``V_Tender`` (an N:M join).

All SQL is library-owned. User values (dates, site IDs, category names) are always ``?``
parameters; the only text formatted into SQL is placeholder strings and library constants.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from sitewatch_data_client._constants import (
    BASE_TYPE_DEPOSIT,
    BASE_TYPE_DISCOUNT,
    BASE_TYPE_PAIDOUT,
    BASE_TYPE_TENDER,
    BASE_TYPES,
    RANGE_LIMITS,
)
from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client._filters import (
    DateInput,
    Sites,
    normalize_date_range,
    normalize_site,
    normalize_sites,
    placeholders,
)
from sitewatch_data_client.types._sales import (
    DailySales,
    HourlySales,
    ItemSales,
    Sale,
    SaleLine,
    SalesTotals,
    SiteMetric,
    StatusCount,
    TenderTotal,
)

if TYPE_CHECKING:
    from sitewatch_data_client._client import SiteWatchClient

GroupBy = Literal["item", "category", "base_type"]
_GROUP_BY_VALUES: tuple[str, ...] = ("item", "category", "base_type")

# ---------------------------------------------------------------------------------------
# SQL builders. Each returns ``(sql, params)``. SQL is kept next to the resource that
# uses it (one module per area), so it can be unit-tested without a database.
# ---------------------------------------------------------------------------------------

_CAR_COUNT_FACT = "TotalCarCount"
_CAR_COUNT_PROFIT_CENTER = 1


def _range_params(sites: list[int], start: datetime, end: datetime) -> list[Any]:
    return [*sites, start, end]


def site_codes_sql(sites: list[int]) -> tuple[str, list[Any]]:
    """``V_Site`` code lookup for the given site IDs (no credential columns)."""
    sql = f"SELECT ID, Code FROM V_Site WHERE ID IN ({placeholders(len(sites))})"
    return sql, list(sites)


def daily_headers_sql(sites: list[int], start: datetime, end: datetime) -> tuple[str, list]:
    """Statement 7.1(a): header totals split into completed and paid-pending."""
    sql = f"""
SELECT s.Site, s.LogDate, f_IsCompleted(s.Status) AS IsCompleted,
       COUNT(*) AS Sales,
       CAST(SUM(s.Total) AS NUMERIC(12,2)) AS SalesTotal
FROM V_Sale s
WHERE s.Site IN ({placeholders(len(sites))}) AND s.LogDate >= ? AND s.LogDate < ?
  AND (f_IsCompleted(s.Status) = 1 OR f_IsIncomplete(s.Status) = 1)
GROUP BY s.Site, s.LogDate, 3
"""
    return sql, _range_params(sites, start, end)


def daily_car_count_sql(sites: list[int], start: datetime, end: datetime) -> tuple[str, list]:
    """Statement 7.1(b): ``TotalCarCount`` fact (profit center 1) per site and day."""
    sql = f"""
SELECT s.Site, s.LogDate, SUM(sf.Data) AS CarCount
FROM V_Sale s
JOIN V_SaleFacts sf ON sf.Site = s.Site AND sf.SaleID = s.ObjID
JOIN V_Fact f ON f.ObjID = sf.Fact
WHERE s.Site IN ({placeholders(len(sites))}) AND s.LogDate >= ? AND s.LogDate < ?
  AND sf.ProfitCenter = ? AND f.Name = ?
GROUP BY s.Site, s.LogDate
"""
    return sql, [*_range_params(sites, start, end), _CAR_COUNT_PROFIT_CENTER, _CAR_COUNT_FACT]


def daily_base_types_sql(sites: list[int], start: datetime, end: datetime) -> tuple[str, list]:
    """Statement 7.1(c): discount, paid-out, deposit and tender totals (completed sales)."""
    base_types = [BASE_TYPE_DISCOUNT, BASE_TYPE_PAIDOUT, BASE_TYPE_DEPOSIT, BASE_TYPE_TENDER]
    sql = f"""
SELECT s.Site, s.LogDate, t.BaseType, CAST(SUM(COALESCE(si.Amt, 0)) AS NUMERIC(12,2)) AS Amt
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
WHERE s.Site IN ({placeholders(len(sites))}) AND s.LogDate >= ? AND s.LogDate < ?
  AND f_IsCompleted(s.Status) = 1 AND t.BaseType IN ({placeholders(len(base_types))})
GROUP BY s.Site, s.LogDate, t.BaseType
"""
    return sql, [*_range_params(sites, start, end), *base_types]


#: Terminal that books the ARM monthly auto-recharges (one per ICON site).
AUTO_RECHARGE_TERMINAL = "Automatic Recharge"


def daily_auto_recharge_sql(sites: list[int], start: datetime, end: datetime) -> tuple[str, list]:
    """Completed sales total booked by the ``Automatic Recharge`` terminal per site and day.

    ARM monthly recharges are posted in a batch around 06:20 on billing days. They are part
    of ``DailySales.revenue`` but not driven by labor, so productivity ratios leave them out.
    """
    sql = f"""
SELECT s.Site, s.LogDate, COUNT(*) AS RechargeSales,
       CAST(SUM(s.Total) AS NUMERIC(12,2)) AS RechargeTotal
FROM V_Sale s
JOIN V_Terminal t ON t.ObjID = s.Terminal AND t.Site = s.Site
WHERE s.Site IN ({placeholders(len(sites))}) AND s.LogDate >= ? AND s.LogDate < ?
  AND f_IsCompleted(s.Status) = 1 AND t.Name = ?
GROUP BY s.Site, s.LogDate
"""
    return sql, [*_range_params(sites, start, end), AUTO_RECHARGE_TERMINAL]


def hourly_sales_sql(sites: list[int], start: datetime, end: datetime) -> tuple[str, list]:
    """Statement 7.2: completed sales and total by paid hour (``V_SaleStats.Hr``)."""
    sql = f"""
SELECT ss.Site, ss.LogDate, ss.Hr, COUNT(*) AS CompletedSales,
       CAST(SUM(s.Total) AS NUMERIC(12,2)) AS SalesTotal
FROM V_SaleStats ss
JOIN V_Sale s ON s.Site = ss.Site AND s.ObjID = ss.SaleID
WHERE ss.Site IN ({placeholders(len(sites))}) AND ss.LogDate >= ? AND ss.LogDate < ?
  AND f_IsCompleted(ss.Status) = 1
GROUP BY ss.Site, ss.LogDate, ss.Hr
"""
    return sql, _range_params(sites, start, end)


def hourly_car_count_sql(sites: list[int], start: datetime, end: datetime) -> tuple[str, list]:
    """Statement 7.2: ``TotalCarCount`` fact (profit center 1) by paid hour."""
    sql = f"""
SELECT ss.Site, ss.LogDate, ss.Hr, SUM(sf.Data) AS CarCount
FROM V_SaleStats ss
JOIN V_SaleFacts sf ON sf.Site = ss.Site AND sf.SaleID = ss.SaleID
JOIN V_Fact f ON f.ObjID = sf.Fact
WHERE ss.Site IN ({placeholders(len(sites))}) AND ss.LogDate >= ? AND ss.LogDate < ?
  AND sf.ProfitCenter = ? AND f.Name = ?
GROUP BY ss.Site, ss.LogDate, ss.Hr
"""
    return sql, [*_range_params(sites, start, end), _CAR_COUNT_PROFIT_CENTER, _CAR_COUNT_FACT]


def category_branch_sql(name: str) -> tuple[str, list]:
    """Statement 7.3a: resolve a report-category name to its ``Branch``."""
    sql = "SELECT rc.Branch FROM V_ItemRptCategory rc WHERE UPPER(TRIM(rc.Name)) = UPPER(?)"
    return sql, [name]


_ITEM_GROUP_COLUMNS: dict[str, tuple[str, str]] = {
    # group_by -> (select columns, group-by columns)
    "item": (
        "i.ObjID AS ItemID, i.Name AS ItemName, rc.Name AS Category, rc.Branch, t.BaseType",
        "i.ObjID, i.Name, rc.Name, rc.Branch, t.BaseType",
    ),
    "category": (
        "rc.Name AS Category, rc.Branch, t.BaseType",
        "rc.Name, rc.Branch, t.BaseType",
    ),
    "base_type": ("t.BaseType", "t.BaseType"),
}


def item_sales_sql(
    sites: list[int],
    start: datetime,
    end: datetime,
    group_by: str,
    branch: str | None = None,
) -> tuple[str, list]:
    """Statement 7.3: SWSLS06-style item / category / base-type mix (completed sales)."""
    select_cols, group_cols = _ITEM_GROUP_COLUMNS[group_by]
    params = _range_params(sites, start, end)
    branch_filter = ""
    if branch is not None:
        branch_filter = "\n  AND rc.Branch STARTING WITH ?"
        params.append(branch)
    sql = f"""
SELECT s.Site, s.LogDate, {select_cols}, COUNT(*) AS Lines,
       CAST(SUM(COALESCE(si.Qty, 0)) AS NUMERIC(12,2)) AS Qty,
       CAST(SUM(COALESCE(si.Amt, 0)) AS NUMERIC(12,2)) AS Amount
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE s.Site IN ({placeholders(len(sites))}) AND s.LogDate >= ? AND s.LogDate < ?
  AND f_IsCompleted(s.Status) = 1{branch_filter}
GROUP BY s.Site, s.LogDate, {group_cols}
"""
    return sql, params


def tenders_sql(
    sites: list[int], start: datetime, end: datetime, by_item: bool = False
) -> tuple[str, list]:
    """Statement 7.4: base type 180 lines, amount = ``-SUM(Amt)`` (completed sales)."""
    item_select = " i.ObjID AS ItemID, i.Name AS ItemName," if by_item else ""
    item_group = ", i.ObjID, i.Name" if by_item else ""
    sql = f"""
SELECT s.Site, s.LogDate, rc.Name AS Category, rc.Branch,{item_select}
       COUNT(*) AS Lines, CAST(-SUM(COALESCE(si.Amt, 0)) AS NUMERIC(12,2)) AS Amount
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE s.Site IN ({placeholders(len(sites))}) AND s.LogDate >= ? AND s.LogDate < ?
  AND f_IsCompleted(s.Status) = 1 AND t.BaseType = ?
GROUP BY s.Site, s.LogDate, rc.Name, rc.Branch{item_group}
"""
    return sql, [*_range_params(sites, start, end), BASE_TYPE_TENDER]


def _sale_where(include_incomplete: bool) -> str:
    where = "s.Site = ? AND s.LogDate >= ? AND s.LogDate < ?"
    if not include_incomplete:
        where += " AND f_IsCompleted(s.Status) = 1"
    return where


def sale_headers_sql(
    site: int, start: datetime, end: datetime, include_incomplete: bool = False
) -> tuple[str, list]:
    """Statement 7.5(a): SWSLS05-style sale headers for one site (no plate text)."""
    sql = f"""
SELECT s.Site, s.ObjID, f_SaleScr(s.ObjID) AS ReceiptNumber, s.Code,
       f_StatusCode(s.Status) AS StatusCode, f_IsCompleted(s.Status) AS IsCompleted,
       s.Created, s.LogDate, s.LogDate + s.LogTime / 14400.0 AS PaidAt, ss.Hr, s.Shift,
       s.Terminal AS TerminalID, t.Name AS TerminalName,
       s.CustomerCode AS CustomerCodeID, cc.Customer AS CustomerID,
       s.ActualSaleID, CAST(s.Total AS NUMERIC(12,2)) AS Total
FROM V_Sale s
JOIN V_SaleStats ss ON ss.Site = s.Site AND ss.SaleID = s.ObjID
LEFT JOIN V_Terminal t ON t.ObjID = s.Terminal
LEFT JOIN V_CustomerCode cc ON cc.ObjID = s.CustomerCode
WHERE {_sale_where(include_incomplete)}
ORDER BY s.LogDate, s.LogTime, s.ObjID
"""
    return sql, [site, start, end]


def sale_lines_sql(
    site: int, start: datetime, end: datetime, include_incomplete: bool = False
) -> tuple[str, list]:
    """Statement 7.5(b): sale lines for the same sales (``si.Note`` is never selected)."""
    sql = f"""
SELECT s.ObjID AS SaleID, si.Seq, si.Leader, si.Item, i.Name AS ItemName,
       rc.Name AS Category, t.BaseType, si.Qty, si.Val,
       CAST(si.Amt AS NUMERIC(12,2)) AS Amt
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE {_sale_where(include_incomplete)}
ORDER BY s.ObjID, si.Seq
"""
    return sql, [site, start, end]


def status_mix_sql(sites: list[int], start: datetime, end: datetime) -> tuple[str, list]:
    """Statement 7.6: sale count and total by status code (paid-date sales only)."""
    sql = f"""
SELECT s.Site, s.LogDate, f_StatusCode(s.Status) AS StatusCode,
       f_IsCompleted(s.Status) AS IsCompleted,
       COUNT(*) AS Sales, CAST(SUM(s.Total) AS NUMERIC(12,2)) AS Total
FROM V_Sale s
WHERE s.Site IN ({placeholders(len(sites))}) AND s.LogDate >= ? AND s.LogDate < ?
GROUP BY s.Site, s.LogDate, 3, 4
"""
    return sql, _range_params(sites, start, end)


# ---------------------------------------------------------------------------------------
# Row mapping helpers
# ---------------------------------------------------------------------------------------


def _is_null(value: Any) -> bool:
    if value is None or value is pd.NaT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        return bool(pd.isna(value)) if not isinstance(value, (str, bytes)) else False
    except (TypeError, ValueError):
        return False


def _money(value: Any) -> float:
    """Round money to 2 dp (mandatory: dialect-1 NUMERIC(12,2) is a double). NULL -> 0."""
    if _is_null(value):
        return 0.0
    if isinstance(value, Decimal):
        value = float(value)
    result = round(float(value), 2)
    return 0.0 if result == 0 else result  # normalize -0.0


def _opt_money(value: Any) -> float | None:
    return None if _is_null(value) else _money(value)


def _opt_float(value: Any) -> float | None:
    if _is_null(value):
        return None
    return round(float(value), 4)


def _int(value: Any) -> int:
    if _is_null(value):
        return 0
    return int(round(float(value)))


def _opt_int(value: Any) -> int | None:
    return None if _is_null(value) else _int(value)


def _text(value: Any) -> str | None:
    if _is_null(value):
        return None
    text = str(value).strip()
    return text or None


def _to_date(value: Any) -> date | None:
    if _is_null(value):
        return None
    if isinstance(value, datetime):  # includes pandas.Timestamp
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _to_datetime(value: Any) -> datetime | None:
    if _is_null(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return pd.Timestamp(value).to_pydatetime()


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.to_dict("records")


class SalesResource:
    """Sales, car counts, item mix, tenders and sale detail.

    Access via ``client.sales``. Every method requires an inclusive date range on the
    business (paid) date and, unless noted, defaults ``sites`` to the ICON car-wash sites
    (5, 13, 18, 20, 24, 25).
    """

    def __init__(self, client: SiteWatchClient) -> None:
        self._client = client
        self._site_code_cache: dict[int, str] = {}

    # -- internal ------------------------------------------------------------------------

    def _site_codes(self, sites: Iterable[int]) -> dict[int, str]:
        """Return ``{site_id: code}`` for ``sites``, querying ``V_Site`` once per new ID."""
        wanted = sorted(set(sites))
        missing = [s for s in wanted if s not in self._site_code_cache]
        if missing:
            sql, params = site_codes_sql(missing)
            for row in _records(self._client._execute(sql, params)):
                site_id = _opt_int(row.get("id"))
                if site_id is not None:
                    self._site_code_cache[site_id] = _text(row.get("code")) or ""
        return {s: self._site_code_cache.get(s, "") for s in wanted}

    # -- daily ---------------------------------------------------------------------------

    def daily_summary(
        self, start: DateInput, end: DateInput, sites: Sites = None
    ) -> list[DailySales]:
        """Daily sales, car count, revenue and line-type totals per site.

        One row per site per business day that has any paid sale. Runs three grouped
        statements (headers, car-count fact, base-type lines) plus a cached ``V_Site`` code
        lookup, and merges them in Python. About 0.5 s per site-day.

        Rules:

        * ``completed_sales`` and ``sales_total`` cover completed sales only
          (``f_IsCompleted = 1``); ``Notes`` rows are never counted.
        * ``car_count`` is the ``TotalCarCount`` fact in profit center 1. It includes paid
          sales whose wash is pending (``I:P-W``), which are reported separately in
          ``pending_wash_sales`` / ``pending_wash_total``.
        * ``revenue = sales_total - deposits``: XPT cash-acceptor deposits (base type 170)
          are cash movements, not revenue. Paid-outs (base type 160, ARM refunds) stay
          inside revenue and are also reported as ``paidouts``.
        * ``tendered`` is ``-SUM(Amt)`` of base type 180 lines.

        Args:
            start: First business date (inclusive): ``date``, ``datetime`` or ISO string.
            end: Last business date (inclusive). The range may span at most 31 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.

        Returns:
            List of :class:`DailySales`, ordered by site then date.

        Raises:
            ValidationError: If the dates or sites are invalid, or the range exceeds 31
                days.
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> rows = client.sales.daily_summary("2026-10-05", "2026-10-11", sites=24)
            >>> sum(r.car_count for r in rows)
        """
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["sales.daily_summary"])

        headers = _records(self._client._execute(*daily_headers_sql(site_ids, start_dt, end_dt)))
        cars = _records(self._client._execute(*daily_car_count_sql(site_ids, start_dt, end_dt)))
        lines = _records(self._client._execute(*daily_base_types_sql(site_ids, start_dt, end_dt)))

        acc: dict[tuple[int, date], dict[str, Any]] = {}

        def bucket(row: dict[str, Any]) -> dict[str, Any]:
            key = (_int(row["site"]), _to_date(row["logdate"]))
            if key not in acc:
                acc[key] = {
                    "completed_sales": 0,
                    "car_count": 0,
                    "sales_total": 0.0,
                    "deposits": 0.0,
                    "paidouts": 0.0,
                    "discounts": 0.0,
                    "tendered": 0.0,
                    "pending_wash_sales": 0,
                    "pending_wash_total": 0.0,
                }
            return acc[key]

        for row in headers:
            b = bucket(row)
            if _int(row.get("iscompleted")) == 1:
                b["completed_sales"] += _int(row.get("sales"))
                b["sales_total"] += _money(row.get("salestotal"))
            else:
                b["pending_wash_sales"] += _int(row.get("sales"))
                b["pending_wash_total"] += _money(row.get("salestotal"))
        for row in cars:
            bucket(row)["car_count"] += _int(row.get("carcount"))
        for row in lines:
            base_type = _int(row.get("basetype"))
            amount = _money(row.get("amt"))
            b = bucket(row)
            if base_type == BASE_TYPE_DISCOUNT:
                b["discounts"] += amount
            elif base_type == BASE_TYPE_PAIDOUT:
                b["paidouts"] += amount
            elif base_type == BASE_TYPE_DEPOSIT:
                b["deposits"] += amount
            elif base_type == BASE_TYPE_TENDER:
                b["tendered"] -= amount

        codes = self._site_codes(site_ids) if acc else {}
        result: list[DailySales] = []
        for (site_id, log_date), b in sorted(acc.items()):
            sales_total = _money(b["sales_total"])
            deposits = _money(b["deposits"])
            result.append(
                DailySales(
                    site_id=site_id,
                    site_code=codes.get(site_id, ""),
                    log_date=log_date,
                    completed_sales=b["completed_sales"],
                    car_count=b["car_count"],
                    sales_total=sales_total,
                    deposits=deposits,
                    revenue=_money(sales_total - deposits),
                    paidouts=_money(b["paidouts"]),
                    discounts=_money(b["discounts"]),
                    tendered=_money(b["tendered"]),
                    pending_wash_sales=b["pending_wash_sales"],
                    pending_wash_total=_money(b["pending_wash_total"]),
                )
            )
        return result

    def totals(self, start: DateInput, end: DateInput, sites: Sites = None) -> SalesTotals:
        """Period totals, summed from :meth:`daily_summary`, with per-site breakdowns.

        Same rules and cost as :meth:`daily_summary` (it runs it once).

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 31 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.

        Returns:
            :class:`SalesTotals`. ``car_count_by_site`` and ``revenue_by_site`` list every
            requested site (0 when it had no sales), ordered by site ID.

        Raises:
            ValidationError: If the dates or sites are invalid, or the range exceeds 31
                days.
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> t = client.sales.totals("2026-10-05", "2026-10-11")
            >>> t.car_count, t.revenue
        """
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["sales.totals"])
        rows = self.daily_summary(start_dt.date(), (end_dt - timedelta(days=1)).date(), site_ids)

        codes = self._site_codes(site_ids)
        cars = {s: 0 for s in site_ids}
        revenue = {s: 0.0 for s in site_ids}
        for r in rows:
            cars[r.site_id] = cars.get(r.site_id, 0) + r.car_count
            revenue[r.site_id] = revenue.get(r.site_id, 0.0) + r.revenue

        return SalesTotals(
            period_start=start_dt.date(),
            period_end=(end_dt - timedelta(days=1)).date(),
            completed_sales=sum(r.completed_sales for r in rows),
            car_count=sum(r.car_count for r in rows),
            sales_total=_money(sum(r.sales_total for r in rows)),
            revenue=_money(sum(r.revenue for r in rows)),
            paidouts=_money(sum(r.paidouts for r in rows)),
            pending_wash_sales=sum(r.pending_wash_sales for r in rows),
            car_count_by_site=[
                SiteMetric(site_id=s, site_code=codes.get(s, ""), value=float(cars[s]))
                for s in sorted(cars)
            ],
            revenue_by_site=[
                SiteMetric(site_id=s, site_code=codes.get(s, ""), value=_money(revenue[s]))
                for s in sorted(revenue)
            ],
        )

    # -- hourly --------------------------------------------------------------------------

    def hourly_summary(
        self, start: DateInput, end: DateInput, sites: Sites = None
    ) -> list[HourlySales]:
        """Completed sales, car count and sales total by paid hour.

        The hour is ``V_SaleStats.Hr`` (never a CAST of ``LogTime``, which rounds in
        dialect 1). Hour 6 is dominated by automatic ARM recharges (about 06:20, terminal
        "Automatic Recharge"), which inflate ``completed_sales`` and ``sales_total`` but not
        ``car_count``. Two statements; about 0.2 s per site-day.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 14 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.

        Returns:
            List of :class:`HourlySales`, ordered by site, date, hour. Hours with neither
            completed sales nor cars are omitted.

        Raises:
            ValidationError: If the dates or sites are invalid, or the range exceeds 14
                days.
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> rows = client.sales.hourly_summary("2026-10-14", "2026-10-14", sites=24)
            >>> busiest = max(rows, key=lambda r: r.car_count)
        """
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["sales.hourly_summary"])

        sales = _records(self._client._execute(*hourly_sales_sql(site_ids, start_dt, end_dt)))
        cars = _records(self._client._execute(*hourly_car_count_sql(site_ids, start_dt, end_dt)))

        acc: dict[tuple[int, date, int], dict[str, Any]] = {}

        def bucket(row: dict[str, Any]) -> dict[str, Any]:
            key = (_int(row["site"]), _to_date(row["logdate"]), _int(row["hr"]))
            return acc.setdefault(key, {"completed_sales": 0, "car_count": 0, "sales_total": 0.0})

        for row in sales:
            b = bucket(row)
            b["completed_sales"] += _int(row.get("completedsales"))
            b["sales_total"] += _money(row.get("salestotal"))
        for row in cars:
            bucket(row)["car_count"] += _int(row.get("carcount"))

        codes = self._site_codes(site_ids) if acc else {}
        return [
            HourlySales(
                site_id=site_id,
                site_code=codes.get(site_id, ""),
                log_date=log_date,
                hour=hour,
                completed_sales=b["completed_sales"],
                car_count=b["car_count"],
                sales_total=_money(b["sales_total"]),
            )
            for (site_id, log_date, hour), b in sorted(acc.items())
        ]

    # -- item mix ------------------------------------------------------------------------

    def _resolve_branch(self, report_category: str) -> str:
        if not isinstance(report_category, str) or not report_category.strip():
            raise ValidationError("report_category must be a non-empty category name.")
        name = report_category.strip()
        rows = _records(self._client._execute(*category_branch_sql(name)))
        branches = [b for b in (_text(r.get("branch")) for r in rows) if b]
        if not branches:
            raise ValidationError(
                f"Unknown report category: {name!r}. "
                "See client.catalog.report_categories() for valid names."
            )
        if len(branches) > 1:
            raise ValidationError(
                f"Report category name {name!r} is ambiguous ({len(branches)} matches)."
            )
        return branches[0]

    def item_sales(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
        *,
        group_by: GroupBy = "item",
        report_category: str | None = None,
        include_zero: bool = False,
    ) -> list[ItemSales]:
        """Item, category or base-type mix for completed sales (SWSLS06 without GL).

        Rows are always split by site and business day. ``V_Item.Enabled`` is not
        filtered, because disabled items still appear on historical sales. GL accounts are
        not available (``V_Item.GlAccount`` is NULL on every item).

        Item names can carry personal names ("Wash Notes" lines), so treat item-level
        results as internal.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 31 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.
            group_by: ``"item"`` (default), ``"category"`` or ``"base_type"``.
            report_category: Optional report-category **name** (case-insensitive). It is
                resolved to its branch and matched with ``STARTING WITH``, so it covers the
                category and every sub-category (``"Wash"`` covers the whole Wash tree,
                ``"Basic Washes"`` gives the wash package mix).
            include_zero: Keep rows whose amount and quantity are both 0 (headers,
                placeholders). Default False.

        Returns:
            List of :class:`ItemSales`, ordered by site, date, category branch, item.

        Raises:
            ValidationError: If dates, sites or ``group_by`` are invalid, the range exceeds
                31 days, or ``report_category`` matches no category (or more than one).
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> mix = client.sales.item_sales(
            ...     "2026-10-05", "2026-10-11", sites=24, report_category="Basic Washes"
            ... )
            >>> discounts = [r for r in client.sales.item_sales(
            ...     "2026-10-05", "2026-10-11", sites=24, group_by="base_type"
            ... ) if r.base_type == 150]
        """
        if group_by not in _GROUP_BY_VALUES:
            raise ValidationError(f"group_by must be one of {_GROUP_BY_VALUES}, got {group_by!r}.")
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["sales.item_sales"])
        branch = self._resolve_branch(report_category) if report_category is not None else None

        rows = _records(
            self._client._execute(*item_sales_sql(site_ids, start_dt, end_dt, group_by, branch))
        )
        codes = self._site_codes(site_ids) if rows else {}
        result: list[ItemSales] = []
        for row in rows:
            amount = _money(row.get("amount"))
            quantity = _opt_float(row.get("qty")) or 0.0
            if not include_zero and amount == 0 and quantity == 0:
                continue
            site_id = _int(row["site"])
            base_type = _int(row.get("basetype"))
            has_item = group_by == "item"
            has_category = group_by != "base_type"
            result.append(
                ItemSales(
                    site_id=site_id,
                    site_code=codes.get(site_id, ""),
                    log_date=_to_date(row["logdate"]),
                    item_id=_opt_int(row.get("itemid")) if has_item else None,
                    item_name=_text(row.get("itemname")) if has_item else None,
                    category=_text(row.get("category")) if has_category else None,
                    category_branch=_text(row.get("branch")) if has_category else None,
                    base_type=base_type,
                    base_type_name=BASE_TYPES.get(base_type, str(base_type)),
                    lines=_int(row.get("lines")),
                    quantity=quantity,
                    amount=amount,
                )
            )
        result.sort(
            key=lambda r: (
                r.site_id,
                r.log_date,
                r.category_branch or "",
                r.base_type,
                r.item_name or "",
                r.item_id or 0,
            )
        )
        return result

    # -- tenders -------------------------------------------------------------------------

    def tenders(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
        *,
        by_item: bool = False,
    ) -> list[TenderTotal]:
        """Payment breakdown: money received by tender category (and item).

        Tender lines are base type 180 on completed sales; ``amount`` is ``-SUM(Amt)`` so
        money received is positive. Tenders are named by report category and item, never
        through ``V_Tender`` (that join is N:M: Visa and MC share one item).

        Caveats: ``lines`` counts tender lines, not customers or transactions. XPT kiosk
        cash appears as separate ``CASH`` and ``CHANGE`` lines, so cash is reliable only at
        category level (``by_item=False``).

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 31 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.
            by_item: Also split by tender item (grouped by item ID, so two items sharing a
                display name stay separate).

        Returns:
            List of :class:`TenderTotal`, ordered by site, date, category, tender.

        Raises:
            ValidationError: If the dates or sites are invalid, or the range exceeds 31
                days.
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> rows = client.sales.tenders("2026-10-05", "2026-10-11", sites=24)
            >>> {r.category: r.amount for r in rows if r.log_date.day == 5}
        """
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["sales.tenders"])
        rows = _records(self._client._execute(*tenders_sql(site_ids, start_dt, end_dt, by_item)))
        codes = self._site_codes(site_ids) if rows else {}
        result = [
            TenderTotal(
                site_id=_int(row["site"]),
                site_code=codes.get(_int(row["site"]), ""),
                log_date=_to_date(row["logdate"]),
                category=_text(row.get("category")) or "",
                category_branch=_text(row.get("branch")) or "",
                tender=_text(row.get("itemname")) if by_item else None,
                lines=_int(row.get("lines")),
                amount=_money(row.get("amount")),
            )
            for row in rows
        ]
        result.sort(
            key=lambda r: (r.site_id, r.log_date, r.category_branch, r.category, r.tender or "")
        )
        return result

    # -- sale detail ---------------------------------------------------------------------

    def list(
        self,
        start: DateInput,
        end: DateInput,
        site: int,
        *,
        include_items: bool = True,
        include_incomplete: bool = False,
    ) -> list[Sale]:
        """Sale detail (SWSLS05 style) for one site, with optional lines.

        Row-level data, so the range is capped at 3 days and one site. About 6 s per
        site-day for the headers; one more statement for the lines.

        Customers are exposed as internal IDs only (``customer_code_id``,
        ``customer_id``), never the plate text. The free-text line ``Note`` column is not
        read. ``E:-P`` attempts never appear, because their ``LogDate`` is NULL.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 3 days.
            site: One site ID (required).
            include_items: Fetch the sale lines (default True).
            include_incomplete: Include every paid-date status (``I:P-W``, ``Notes``,
                ``PendAppr``) for diagnostics. Default False (completed sales only).

        Returns:
            List of :class:`Sale`, ordered by paid time, each with ``items`` ordered by
            ``seq``.

        Raises:
            ValidationError: If the dates or site are invalid, or the range exceeds 3 days.
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> sales = client.sales.list("2026-10-14", "2026-10-14", site=24)
            >>> recharges = [s for s in sales if s.terminal_name == "Automatic Recharge"]
        """
        site_id = normalize_site(site)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["sales.list"])

        headers = _records(
            self._client._execute(*sale_headers_sql(site_id, start_dt, end_dt, include_incomplete))
        )
        if not headers:
            return []

        lines_by_sale: dict[int, list[SaleLine]] = {}
        if include_items:
            line_rows = _records(
                self._client._execute(
                    *sale_lines_sql(site_id, start_dt, end_dt, include_incomplete)
                )
            )
            for row in line_rows:
                base_type = _int(row.get("basetype"))
                line = SaleLine(
                    seq=_int(row.get("seq")),
                    leader_seq=_opt_int(row.get("leader")),
                    item_id=_int(row.get("item")),
                    item_name=_text(row.get("itemname")),
                    category=_text(row.get("category")),
                    base_type=base_type,
                    is_tender=base_type == BASE_TYPE_TENDER,
                    quantity=_opt_float(row.get("qty")),
                    unit_value=_opt_money(row.get("val")),
                    amount=_opt_money(row.get("amt")),
                )
                lines_by_sale.setdefault(_int(row.get("saleid")), []).append(line)
            for items in lines_by_sale.values():
                items.sort(key=lambda x: x.seq)

        code = self._site_codes([site_id]).get(site_id, "")
        result: list[Sale] = []
        for row in headers:
            sale_id = _int(row.get("objid"))
            receipt = row.get("receiptnumber")
            result.append(
                Sale(
                    site_id=_int(row.get("site")) or site_id,
                    site_code=code,
                    sale_id=sale_id,
                    receipt_number=(
                        str(_int(receipt))
                        if isinstance(receipt, (int, float, Decimal)) and not _is_null(receipt)
                        else (_text(receipt) or "")
                    ),
                    code=_text(row.get("code")),
                    status_code=_text(row.get("statuscode")) or "",
                    is_completed=_int(row.get("iscompleted")) == 1,
                    created_at=_to_datetime(row.get("created")),
                    log_date=_to_date(row.get("logdate")),
                    paid_at=_to_datetime(row.get("paidat")),
                    hour=_opt_int(row.get("hr")),
                    shift=_opt_int(row.get("shift")),
                    terminal_id=_opt_int(row.get("terminalid")),
                    terminal_name=_text(row.get("terminalname")),
                    customer_code_id=_opt_int(row.get("customercodeid")),
                    customer_id=_opt_int(row.get("customerid")),
                    actual_sale_id=_opt_int(row.get("actualsaleid")),
                    total=_money(row.get("total")),
                    items=lines_by_sale.get(sale_id, []),
                )
            )
        return result

    # -- diagnostics ---------------------------------------------------------------------

    def status_mix(
        self, start: DateInput, end: DateInput, sites: Sites = None
    ) -> list[StatusCount]:
        """Sale count and total by status code per site and day.

        Shows what the completed-only filter removes from **paid-date** sales: ``Notes``
        rows (ARM declines and expiries, with a nonzero ``Total``), ``I:P-W`` (paid, wash
        pending) and ``PendAppr``. ``E:-P`` attempts never appear, because their
        ``LogDate`` is NULL. One statement; about 0.5 s per site-day.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 14 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.

        Returns:
            List of :class:`StatusCount`, ordered by site, date, status code.

        Raises:
            ValidationError: If the dates or sites are invalid, or the range exceeds 14
                days.
            ConnectionError: If the client is not connected.
            QueryError: If a query fails.

        Examples:
            >>> mix = client.sales.status_mix("2026-10-14", "2026-10-14", sites=24)
            >>> {m.status_code: m.sales for m in mix}
        """
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["sales.status_mix"])
        rows = _records(self._client._execute(*status_mix_sql(site_ids, start_dt, end_dt)))
        codes = self._site_codes(site_ids) if rows else {}
        result = [
            StatusCount(
                site_id=_int(row["site"]),
                site_code=codes.get(_int(row["site"]), ""),
                log_date=_to_date(row["logdate"]),
                status_code=_text(row.get("statuscode")) or "",
                is_completed=_int(row.get("iscompleted")) == 1,
                sales=_int(row.get("sales")),
                total=_money(row.get("total")),
            )
            for row in rows
        ]
        result.sort(key=lambda r: (r.site_id, r.log_date, r.status_code))
        return result
