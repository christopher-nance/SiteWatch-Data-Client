"""Memberships resource: ARM (monthly pass) plans, activity, new members and events."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd

from sitewatch_data_client._constants import ARM_PLAN_TYP, RANGE_LIMITS
from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client._filters import (
    DateInput,
    Sites,
    normalize_date,
    normalize_date_range,
    normalize_site,
    normalize_sites,
    placeholders,
)
from sitewatch_data_client.types._memberships import (
    MemberContact,
    MembershipActivity,
    MembershipActivityTotals,
    MembershipEvent,
    MembershipPlan,
    MemberStatus,
    NewMember,
    SiteMetric,
)

if TYPE_CHECKING:
    from sitewatch_data_client._client import SiteWatchClient

# ---------------------------------------------------------------------------------------------
# SQL builders. Each returns (sql, params). Only placeholders and library constants are
# formatted into the SQL text; every caller value is a ``?`` parameter.
# ---------------------------------------------------------------------------------------------

#: Paid sale rule (completed or paid-but-wash-pending ``I:P-W``), section 7.0 of the spec.
_PAID = "(f_IsCompleted({s}.Status) = 1 OR f_IsIncomplete({s}.Status) = 1)"


def site_codes_sql(site_ids: list[int]) -> tuple[str, list[Any]]:
    """``V_Site`` code lookup (explicit columns only: ``V_Site`` holds credentials)."""
    sql = f"SELECT ID, Code FROM V_Site WHERE ID IN ({placeholders(len(site_ids))})"
    return sql, list(site_ids)


def plans_sql(include_disabled: bool = False) -> tuple[str, list[Any]]:
    """ARM plan catalog from ``V_PlanType``."""
    sql = (
        "SELECT pt.ObjID, pt.Name, pt.Enabled, pt.ExpirationAge, pt.RechargePlan, pt.Modified\n"
        "FROM V_PlanType pt\n"
        "WHERE pt.Typ = ?"
    )
    params: list[Any] = [ARM_PLAN_TYP]
    if not include_disabled:
        sql += " AND pt.Enabled = 1"
    sql += "\nORDER BY pt.Name, pt.ObjID"
    return sql, params


def activity_sql(
    site_ids: list[int], start_dt: datetime, end_dt: datetime, by_plan: bool = True
) -> tuple[str, list[Any]]:
    """Daily ARM roll-up from ``V_ARM_Monthly_Activity`` (daily grain despite the name)."""
    plan_cols = "a.PlanType, pt.Name AS PlanName,\n       " if by_plan else ""
    plan_join = "LEFT JOIN V_PlanType pt ON pt.ObjID = a.PlanType\n" if by_plan else ""
    plan_group = ", a.PlanType, pt.Name" if by_plan else ""
    plan_order = ", pt.Name, a.PlanType" if by_plan else ""
    sql = (
        f"SELECT a.Site, a.LogDate, {plan_cols}"
        "SUM(a.SaleCount) AS Joins, CAST(SUM(a.SaleAmount) AS NUMERIC(12,2)) AS JoinAmount,\n"
        "       SUM(a.RechargeCount) AS Recharges,"
        " CAST(SUM(a.RechargeAmount) AS NUMERIC(12,2)) AS RechargeAmount,\n"
        "       SUM(a.UpsellCount) AS Upsells,"
        " CAST(SUM(a.UpsellAmount) AS NUMERIC(12,2)) AS UpsellAmount,\n"
        "       SUM(a.WashUsed) AS WashesUsed,\n"
        "       SUM(a.TransferInCount) AS TransfersIn, SUM(a.TransferOutCount) AS TransfersOut,\n"
        "       SUM(a.SuspendedCount) AS Suspended, SUM(a.ResumedCount) AS Resumed,\n"
        "       SUM(a.TerminatedCount) AS Terminated,"
        " CAST(SUM(a.TerminatedAmount) AS NUMERIC(12,2)) AS TerminatedAmount,\n"
        "       SUM(a.Expired) AS Expired, SUM(a.Discontinuing) AS Discontinuing,"
        " SUM(a.Discontinued) AS Discontinued,\n"
        "       SUM(a.Declined) AS Declined, SUM(a.CardExpiring) AS CardExpiring,"
        " SUM(a.RechargeProblem) AS RechargeProblems\n"
        "FROM V_ARM_Monthly_Activity a\n"
        f"{plan_join}"
        f"WHERE a.Site IN ({placeholders(len(site_ids))}) AND a.LogDate >= ? AND a.LogDate < ?\n"
        f"GROUP BY a.Site, a.LogDate{plan_group}\n"
        f"ORDER BY a.Site, a.LogDate{plan_order}"
    )
    return sql, [*site_ids, start_dt, end_dt]


def new_members_sql(
    site_ids: list[int],
    start_dt: datetime,
    end_dt: datetime,
    as_of_dt: datetime | None,
    include_contact: bool = False,
    include_recharge_site: bool = False,
) -> tuple[str, list[Any]]:
    """New ARM members (port of ``export_members.ps1``, paid-join rule, no card data).

    One row per customer with a paid ARM join at ``site_ids`` in the window. ``JoinSite`` is
    the site of the customer's earliest in-window join (by ``LogDate`` and ``LogTime``), and
    ``PriorJoins`` counts the customer's paid ARM joins at any site before ``JoinDate``
    (rejoins have ``PriorJoins > 0``).
    """
    recharge = ""
    if include_recharge_site:
        recharge = (
            ",\n       (SELECT FIRST 1 p4.Site FROM V_SalePasses p4"
            " JOIN V_Sale s4 ON s4.Site = p4.Site AND s4.ObjID = p4.SaleID\n"
            "         WHERE p4.Customer = j.Customer AND p4.Status IN (1, 3)\n"
            f"           AND {_PAID.format(s='s4')}\n"
            "         ORDER BY s4.LogDate DESC, s4.LogTime DESC) AS RechargeSite"
        )
    contact_cols = ""
    contact_join = ""
    if include_contact:
        contact_cols = (
            ",\n       c.FirstName, c.LastName, c.Name AS FullName, c.Company, c.Address1,"
            " c.Address2, c.City, c.State, c.ZipCode,\n"
            "       c.MainPhone, c.AltPhone, c.Email, c.LastCustCode, c.SuppressMail,"
            " c.Created AS CustomerCreated,\n"
            "       (SELECT LIST(TRIM(cc.Code), ' ') FROM V_CustomerCode cc"
            " WHERE cc.Customer = c.ObjID) AS Codes"
        )
        contact_join = "LEFT JOIN V_Customer c ON c.ObjID = j.Customer\n"
    in_sites = placeholders(len(site_ids))
    sql = (
        "SELECT j.Customer, j.JoinDate,\n"
        "       (SELECT FIRST 1 p5.Site FROM V_SalePasses p5"
        " JOIN V_Sale s5 ON s5.Site = p5.Site AND s5.ObjID = p5.SaleID"
        " JOIN V_PlanType pt5 ON pt5.ObjID = p5.PlanType\n"
        "         WHERE p5.Customer = j.Customer AND p5.Status = 1 AND pt5.Typ = ?\n"
        f"           AND {_PAID.format(s='s5')}\n"
        f"           AND s5.Site IN ({in_sites}) AND s5.LogDate >= ? AND s5.LogDate < ?\n"
        "         ORDER BY s5.LogDate, s5.LogTime, p5.Site) AS JoinSite,\n"
        "       (SELECT COUNT(*) FROM V_SalePasses p6"
        " JOIN V_Sale s6 ON s6.Site = p6.Site AND s6.ObjID = p6.SaleID"
        " JOIN V_PlanType pt6 ON pt6.ObjID = p6.PlanType\n"
        "         WHERE p6.Customer = j.Customer AND p6.Status = 1 AND pt6.Typ = ?\n"
        f"           AND {_PAID.format(s='s6')} AND s6.LogDate < j.JoinDate) AS PriorJoins,\n"
        "       m.FirstSaleSite, m.PlanType, cp.Name AS PlanName, m.PassStatus,"
        " f_ArmStatusName(m.PassStatus) AS PassStatusName,\n"
        "       CAST(m.Amount AS NUMERIC(9,2)) AS BillingAmount, m.Expires, m.MemberSince,"
        " m.Members,\n"
        "       (SELECT MAX(s2.LogDate) FROM V_SalePasses p2"
        " JOIN V_Sale s2 ON s2.Site = p2.Site AND s2.ObjID = p2.SaleID\n"
        "         WHERE p2.Customer = j.Customer AND p2.Status = 0) AS LastUsed,\n"
        "       (SELECT COUNT(*) FROM V_SalePasses p2"
        " JOIN V_Sale s2 ON s2.Site = p2.Site AND s2.ObjID = p2.SaleID\n"
        "         WHERE p2.Customer = j.Customer AND p2.Status = 0"
        " AND s2.LogDate >= j.JoinDate) AS UsesSinceJoin,\n"
        "       (SELECT MAX(s3.LogDate) FROM V_SalePasses p3"
        " JOIN V_Sale s3 ON s3.Site = p3.Site AND s3.ObjID = p3.SaleID\n"
        "         WHERE p3.Customer = j.Customer AND p3.Status IN (1, 3)\n"
        f"           AND {_PAID.format(s='s3')}) AS LastCharge"
        f"{recharge}{contact_cols}\n"
        "FROM (SELECT p.Customer, MIN(s.LogDate) AS JoinDate\n"
        "        FROM V_SalePasses p\n"
        "        JOIN V_Sale s      ON s.Site = p.Site AND s.ObjID = p.SaleID\n"
        "        JOIN V_PlanType pt ON pt.ObjID = p.PlanType\n"
        "       WHERE p.Status = 1 AND pt.Typ = ?\n"
        f"         AND {_PAID.format(s='s')}\n"
        f"         AND s.Site IN ({in_sites}) AND s.LogDate >= ? AND s.LogDate < ?\n"
        "       GROUP BY p.Customer) j\n"
        f"{contact_join}"
        "LEFT JOIN Arm_SalePass_Member(j.Customer, ?) m ON 1 = 1\n"
        "LEFT JOIN V_PlanType cp ON cp.ObjID = m.PlanType\n"
        "ORDER BY j.JoinDate, j.Customer"
    )
    return sql, [
        ARM_PLAN_TYP,
        *site_ids,
        start_dt,
        end_dt,
        ARM_PLAN_TYP,
        ARM_PLAN_TYP,
        *site_ids,
        start_dt,
        end_dt,
        as_of_dt,
    ]


def member_status_sql(customer_id: int, as_of_dt: datetime | None) -> tuple[str, list[Any]]:
    """One member's ARM state through ``Arm_SalePass_Member`` (no PII)."""
    sql = (
        "SELECT m.Leader, m.Member, m.Site, m.SaleID, m.PlanType, pt.Name AS PlanName,"
        " m.PassStatus,\n"
        "       f_ArmStatusName(m.PassStatus) AS PassStatusName,"
        " f_StatusCode(m.SaleStatus) AS SaleStatusCode, m.SalePaidTS,\n"
        "       m.WashesSold, m.WashesUsed, m.WashesLeft, m.Expires, m.FirstSaleSite,"
        " m.MemberSince, m.Members,\n"
        "       CAST(m.Amount AS NUMERIC(9,2)) AS Amount,"
        " CAST(m.Upsell AS NUMERIC(9,2)) AS Upsell\n"
        "FROM Arm_SalePass_Member(?, ?) m\n"
        "LEFT JOIN V_PlanType pt ON pt.ObjID = m.PlanType"
    )
    return sql, [customer_id, as_of_dt]


def events_sql(
    site_id: int, start_dt: datetime, end_dt: datetime, statuses: list[int] | None = None
) -> tuple[str, list[Any]]:
    """Row-level ARM events (``V_SalePasses``) for one site, bounded through ``V_Sale``."""
    status_filter = ""
    params: list[Any] = [site_id, start_dt, end_dt]
    if statuses:
        status_filter = f" AND sp.Status IN ({placeholders(len(statuses))})"
        params.extend(statuses)
    sql = (
        "SELECT sp.Site, sp.SaleID, sp.Seq, s.LogDate,"
        " s.LogDate + s.LogTime / 14400.0 AS EventAt,\n"
        "       f_StatusCode(s.Status) AS SaleStatusCode, sp.Customer, sp.Leader, sp.PlanType,"
        " pt.Name AS PlanName,\n"
        "       sp.Status, f_ArmStatusName(sp.Status) AS StatusName, sp.Washes, sp.Expires,"
        " sp.Members,\n"
        "       CAST(sp.Amount AS NUMERIC(9,2)) AS Amount,"
        " CAST(sp.Upsell AS NUMERIC(9,2)) AS Upsell,"
        " sp.FirstSaleSite\n"
        "FROM V_Sale s\n"
        "JOIN V_SalePasses sp ON sp.Site = s.Site AND sp.SaleID = s.ObjID\n"
        "JOIN V_PlanType pt ON pt.ObjID = sp.PlanType\n"
        f"WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ?{status_filter}\n"
        "ORDER BY s.LogDate, s.LogTime, sp.SaleID, sp.Seq"
    )
    return sql, params


# ---------------------------------------------------------------------------------------------
# Row mapping helpers
# ---------------------------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    """NaN/NaT/None to None; trimmed text with empty strings as None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NaT:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def _int(value: Any) -> int | None:
    v = _clean(value)
    return None if v is None else int(v)


def _int0(value: Any) -> int:
    v = _int(value)
    return 0 if v is None else v


def _money(value: Any) -> float | None:
    """Money: Decimal or noisy float to a float rounded to 2 dp (mandatory, spec section 5)."""
    v = _clean(value)
    if v is None:
        return None
    if isinstance(v, Decimal):
        v = float(v)
    return round(float(v), 2)


def _money0(value: Any) -> float:
    v = _money(value)
    return 0.0 if v is None else v


def _datetime(value: Any) -> datetime | None:
    v = _clean(value)
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return pd.Timestamp(v).to_pydatetime()


def _date(value: Any) -> date | None:
    v = _datetime(value)
    return None if v is None else v.date()


def _str(value: Any) -> str | None:
    v = _clean(value)
    return None if v is None else str(v)


def _rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.to_dict("records")


def _end_of_day(as_of: DateInput | None) -> datetime | None:
    """``as_of`` is end-of-day: a date is sent as ``23:59:59`` of that day, never midnight."""
    d = normalize_date(as_of)
    if d is None:
        return None
    return datetime.combine(d, time(23, 59, 59))


def _normalize_statuses(statuses: Iterable[int] | int | None) -> list[int] | None:
    if statuses is None:
        return None
    if isinstance(statuses, (str, bytes)):
        raise ValidationError("statuses must be an int or an iterable of ints, got str.")
    if isinstance(statuses, int) and not isinstance(statuses, bool):
        statuses = [statuses]
    try:
        items = list(statuses)  # type: ignore[arg-type]
    except TypeError:
        raise ValidationError(
            f"statuses must be an int or an iterable of ints, got {type(statuses).__name__}."
        ) from None
    if not items:
        raise ValidationError("statuses cannot be empty. Pass None for every status.")
    for v in items:
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValidationError(f"Invalid ARM status code: {v!r}. Expected a non-negative int.")
    return sorted(set(items))


class MembershipsResource:
    """ARM (monthly pass) membership methods.

    Access via ``client.memberships``. History methods require a bounded date range and
    default ``sites`` to the ICON car-wash sites (5, 13, 18, 20, 24, 25).
    """

    def __init__(self, client: SiteWatchClient) -> None:
        self._client = client
        self._site_code_cache: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _site_codes(self, site_ids: Iterable[int]) -> dict[int, str]:
        """Cached ``V_Site.Code`` lookup. Unknown IDs map to ``str(site_id)``."""
        wanted = sorted({int(s) for s in site_ids if s is not None})
        missing = [s for s in wanted if s not in self._site_code_cache]
        if missing:
            sql, params = site_codes_sql(missing)
            for r in _rows(self._client._execute(sql, params)):
                code = _str(r.get("code"))
                if code is not None:
                    self._site_code_cache[int(r["id"])] = code
        return {s: self._site_code_cache.get(s, str(s)) for s in wanted}

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    def plans(self, *, include_disabled: bool = False) -> list[MembershipPlan]:
        """List the ARM plan catalog (``V_PlanType`` with ``Typ = 110``).

        Plan types are global (``V_PlanType`` has no site column), so legacy plans appear
        too. The view has no price; a join's amount comes from the sale (see
        ``new_members`` and ``events``). Cheap: one small catalog read.

        Args:
            include_disabled: Also return disabled plans (``Enabled = 2``).

        Returns:
            Plans ordered by name.

        Examples:
            >>> for plan in client.memberships.plans():
            ...     print(plan.plan_type_id, plan.name)
        """
        sql, params = plans_sql(include_disabled)
        df = self._client._execute(sql, params)
        return [
            MembershipPlan(
                plan_type_id=int(r["objid"]),
                name=_str(r.get("name")) or "",
                enabled=_int(r.get("enabled")) == 1,
                expiration_age_days=_int(r.get("expirationage")),
                recharge_plan_id=_int(r.get("rechargeplan")) or None,
                modified_at=_datetime(r.get("modified")),
            )
            for r in _rows(df)
        ]

    # ------------------------------------------------------------------
    # Activity (daily roll-up)
    # ------------------------------------------------------------------

    def activity(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
        *,
        by_plan: bool = True,
    ) -> list[MembershipActivity]:
        """Daily ARM roll-up per site (and per plan) from ``V_ARM_Monthly_Activity``.

        ``V_ARM_Monthly_Activity`` is **daily** despite its name, with one row per leader
        who had activity that day, so it is not an active-member count. Its counters equal
        ``V_SalePasses`` counts on **paid** sales (completed or ``I:P-W``): joins = Status 1,
        recharges = Status 3, washes used = Status 0, declines = Status 120 and so on.

        Performance: cheap. 14 days at all six ICON sites aggregates in about 1 s.

        Args:
            start: First business date (inclusive). ``date``, ``datetime`` or ISO string.
            end: Last business date (inclusive). The range may span at most 93 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.
            by_plan: Group by plan as well as site and date. False gives one row per site
                and day with ``plan_type_id``/``plan_name`` set to None.

        Returns:
            Rows ordered by site, date and plan name. Days without activity are absent.

        Raises:
            ValidationError: Invalid dates or sites, or a range longer than 93 days.

        Examples:
            >>> rows = client.memberships.activity("2026-10-05", "2026-10-11", sites=24)
            >>> sum(r.joins for r in rows)
        """
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["memberships.activity"])
        sql, params = activity_sql(site_ids, start_dt, end_dt, by_plan)
        rows = _rows(self._client._execute(sql, params))
        codes = self._site_codes(site_ids) if rows else {}
        out: list[MembershipActivity] = []
        for r in rows:
            site_id = int(r["site"])
            out.append(
                MembershipActivity(
                    site_id=site_id,
                    site_code=codes.get(site_id, str(site_id)),
                    log_date=_date(r["logdate"]),
                    plan_type_id=_int(r.get("plantype")) if by_plan else None,
                    plan_name=_str(r.get("planname")) if by_plan else None,
                    joins=_int0(r.get("joins")),
                    join_amount=_money0(r.get("joinamount")),
                    recharges=_int0(r.get("recharges")),
                    recharge_amount=_money0(r.get("rechargeamount")),
                    upsells=_int0(r.get("upsells")),
                    upsell_amount=_money0(r.get("upsellamount")),
                    washes_used=_int0(r.get("washesused")),
                    transfers_in=_int0(r.get("transfersin")),
                    transfers_out=_int0(r.get("transfersout")),
                    suspended=_int0(r.get("suspended")),
                    resumed=_int0(r.get("resumed")),
                    terminated=_int0(r.get("terminated")),
                    terminated_amount=_money0(r.get("terminatedamount")),
                    expired=_int0(r.get("expired")),
                    discontinuing=_int0(r.get("discontinuing")),
                    discontinued=_int0(r.get("discontinued")),
                    declined=_int0(r.get("declined")),
                    card_expiring=_int0(r.get("cardexpiring")),
                    recharge_problems=_int0(r.get("rechargeproblems")),
                )
            )
        return out

    def activity_totals(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
    ) -> MembershipActivityTotals:
        """Sum ``activity()`` over the period, with joins broken down by site.

        One roll-up query (``by_plan=False``), summed in Python. Same 93-day limit and
        paid-sale semantics as ``activity()``, so ``totals.joins`` reconciles with
        ``len(new_members(...))`` for the same sites and window.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 93 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.

        Returns:
            Period totals. ``joins_by_site`` lists every requested site, 0 when idle.

        Raises:
            ValidationError: Invalid dates or sites, or a range longer than 93 days.

        Examples:
            >>> t = client.memberships.activity_totals("2026-10-01", "2026-10-14")
            >>> t.joins, [(m.site_code, m.value) for m in t.joins_by_site]
        """
        site_ids = normalize_sites(sites)
        rows = self.activity(start, end, site_ids, by_plan=False)
        codes = self._site_codes(site_ids)
        joins_by: dict[int, int] = {s: 0 for s in site_ids}
        for r in rows:
            joins_by[r.site_id] = joins_by.get(r.site_id, 0) + r.joins
        return MembershipActivityTotals(
            period_start=normalize_date(start),
            period_end=normalize_date(end),
            joins=sum(r.joins for r in rows),
            recharges=sum(r.recharges for r in rows),
            recharge_amount=round(sum(r.recharge_amount for r in rows), 2),
            terminated=sum(r.terminated for r in rows),
            expired=sum(r.expired for r in rows),
            discontinued=sum(r.discontinued for r in rows),
            declined=sum(r.declined for r in rows),
            joins_by_site=[
                SiteMetric(site_id=s, site_code=codes.get(s, str(s)), value=float(v))
                for s, v in sorted(joins_by.items())
            ],
        )

    # ------------------------------------------------------------------
    # New members
    # ------------------------------------------------------------------

    def new_members(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
        *,
        include_contact: bool = False,
        include_recharge_site: bool = False,
        as_of: DateInput | None = None,
        first_join_only: bool = False,
    ) -> list[NewMember]:
        """Customers with a paid ARM join (including rejoins) at ``sites`` in the window.

        Port of ``export_members.ps1`` with a site filter, parameterized dates, no card
        fields and personal data only on request. A join is a ``V_SalePasses`` row with
        ``Status = 1`` on a **paid** sale (completed or ``I:P-W``) of an ARM plan
        (``Typ = 110``). Current plan, status, billing amount and next bill date come from
        ``Arm_SalePass_Member(customer, as_of)``.

        Returning members count: about one in five joins in a week is a rejoin by a
        customer who was a member before. ``prior_joins`` and ``is_rejoin`` tell them apart,
        and ``first_join_only=True`` keeps only customers joining for the first time ever.

        A customer appears once even with several joins in the window (at one or several of
        the requested sites); ``join_date`` and ``join_site_id`` describe the earliest one.
        So ``len(new_members(...))`` equals ``activity_totals(...).joins`` only when no
        customer joined more than once in the window.

        Performance: about 3 s for one site over 14 days (around 60 members). A 31-day,
        six-site call (about 1,000 joins) takes roughly a minute. For interactive use, query
        one site or a window of 14 days or less.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 31 days.
            sites: Site ID or IDs. None means the ICON car-wash sites.
            include_contact: Return name, address, phone, email and plates in
                ``contact``. This is personal data; keep it out of logs and shared files.
            include_recharge_site: Also look up the site of the latest paid join or
                recharge (an extra correlated subquery per member).
            as_of: Date for the current-state snapshot, sent as **23:59:59** of that day.
                None means "now" on the server. Pass ``as_of=end`` for a reproducible
                snapshot as of the close of the window.
            first_join_only: Drop rejoins (customers with an earlier paid ARM join at any
                site), leaving first-time members only.

        Returns:
            Members ordered by join date, then customer ID.

        Raises:
            ValidationError: Invalid dates or sites, or a range longer than 31 days.

        Examples:
            >>> members = client.memberships.new_members(
            ...     "2026-10-05", "2026-10-11", sites=24, as_of="2026-10-11"
            ... )
            >>> len(members), members[0].plan_name
        """
        site_ids = normalize_sites(sites)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["memberships.new_members"])
        sql, params = new_members_sql(
            site_ids,
            start_dt,
            end_dt,
            _end_of_day(as_of),
            include_contact=include_contact,
            include_recharge_site=include_recharge_site,
        )
        rows = _rows(self._client._execute(sql, params))
        if first_join_only:
            rows = [r for r in rows if not _int0(r.get("priorjoins"))]
        if not rows:
            return []
        lookup_ids = set(site_ids)
        lookup_ids.update(int(r["joinsite"]) for r in rows)
        if include_recharge_site:
            lookup_ids.update(i for r in rows if (i := _int(r.get("rechargesite"))) is not None)
        codes = self._site_codes(lookup_ids)
        out: list[NewMember] = []
        for r in rows:
            join_site = int(r["joinsite"])
            recharge_site = _int(r.get("rechargesite")) if include_recharge_site else None
            contact = None
            if include_contact:
                codes_text = _str(r.get("codes"))
                suppress = _int(r.get("suppressmail"))
                contact = MemberContact(
                    first_name=_str(r.get("firstname")),
                    last_name=_str(r.get("lastname")),
                    full_name=_str(r.get("fullname")),
                    company=_str(r.get("company")),
                    address1=_str(r.get("address1")),
                    address2=_str(r.get("address2")),
                    city=_str(r.get("city")),
                    state=_str(r.get("state")),
                    zip=_str(r.get("zipcode")),
                    main_phone=_str(r.get("mainphone")),
                    alt_phone=_str(r.get("altphone")),
                    email=_str(r.get("email")),
                    last_code=_str(r.get("lastcustcode")),
                    codes=codes_text.split() if codes_text else [],
                    suppress_mail=None if suppress is None else suppress == 1,
                    customer_created_at=_datetime(r.get("customercreated")),
                )
            out.append(
                NewMember(
                    customer_id=int(r["customer"]),
                    join_date=_date(r["joindate"]),
                    join_site_id=join_site,
                    join_site_code=codes.get(join_site, str(join_site)),
                    prior_joins=_int0(r.get("priorjoins")),
                    is_rejoin=_int0(r.get("priorjoins")) > 0,
                    first_sale_site_id=_int(r.get("firstsalesite")),
                    plan_type_id=_int(r.get("plantype")),
                    plan_name=_str(r.get("planname")),
                    pass_status=_int(r.get("passstatus")),
                    pass_status_name=_str(r.get("passstatusname")),
                    billing_amount=_money(r.get("billingamount")),
                    next_bill_date=_date(r.get("expires")),
                    member_since=_date(r.get("membersince")),
                    family_members=_int(r.get("members")),
                    last_used_date=_date(r.get("lastused")),
                    uses_since_join=_int0(r.get("usessincejoin")),
                    last_charge_date=_date(r.get("lastcharge")),
                    recharge_site_id=recharge_site,
                    recharge_site_code=(
                        codes.get(recharge_site, str(recharge_site))
                        if recharge_site is not None
                        else None
                    ),
                    contact=contact,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Member status
    # ------------------------------------------------------------------

    def member_status(
        self, customer_id: int, as_of: DateInput | None = None
    ) -> MemberStatus | None:
        """One member's ARM state through the ``Arm_SalePass_Member`` procedure.

        Returns IDs, plan, status, washes and billing fields only; no personal data. The
        procedure returns exactly one row per member, so None means the customer has no ARM
        membership (as of ``as_of``).

        Performance: under a second for a new member, but it grows with the member's
        history. Do not loop it over hundreds of members; use ``new_members`` instead.

        Args:
            customer_id: ``V_Customer.ObjID`` (positive int).
            as_of: Snapshot date, sent as **23:59:59** of that day. None means "now".

        Returns:
            The member's status, or None when the procedure returns no membership.

        Raises:
            ValidationError: If ``customer_id`` is not a positive int or ``as_of`` is invalid.

        Examples:
            >>> status = client.memberships.member_status(123456, as_of="2026-10-14")
            >>> status.plan_name if status else "not a member"
        """
        if isinstance(customer_id, bool) or not isinstance(customer_id, int) or customer_id <= 0:
            raise ValidationError(f"Invalid customer_id: {customer_id!r}. Expected a positive int.")
        sql, params = member_status_sql(customer_id, _end_of_day(as_of))
        rows = _rows(self._client._execute(sql, params))
        rows = [
            r
            for r in rows
            if any(_clean(r.get(k)) is not None for k in ("saleid", "plantype", "passstatus"))
        ]
        if not rows:
            return None
        r = rows[0]
        return MemberStatus(
            customer_id=_int(r.get("member")) or customer_id,
            leader_id=_int(r.get("leader")),
            site_id=_int(r.get("site")),
            sale_id=_int(r.get("saleid")),
            plan_type_id=_int(r.get("plantype")),
            plan_name=_str(r.get("planname")),
            pass_status=_int(r.get("passstatus")),
            pass_status_name=_str(r.get("passstatusname")),
            sale_status_code=_str(r.get("salestatuscode")),
            sale_paid_at=_datetime(r.get("salepaidts")),
            washes_sold=_int(r.get("washessold")),
            washes_used=_int(r.get("washesused")),
            washes_left=_int(r.get("washesleft")),
            expires=_date(r.get("expires")),
            first_sale_site_id=_int(r.get("firstsalesite")),
            member_since=_date(r.get("membersince")),
            family_members=_int(r.get("members")),
            amount=_money(r.get("amount")),
            upsell=_money(r.get("upsell")),
        )

    # ------------------------------------------------------------------
    # Events (row level)
    # ------------------------------------------------------------------

    def events(
        self,
        start: DateInput,
        end: DateInput,
        site: int,
        *,
        statuses: Iterable[int] | int | None = None,
    ) -> list[MembershipEvent]:
        """Row-level ARM events (``V_SalePasses``) for one site: the drill-down behind
        ``activity()``.

        Every sale status is included: joins and uses are on ``C:PW`` or ``I:P-W`` sales,
        recharges and terminations on ``C:P``, declines, expiries and discontinues on
        ``Notes``. Use ``sale_status_code`` to filter. IDs only, no personal data.

        Duplicate rows per sale are **kept** (``Seq`` can tie). SiteWatch reports use the
        highest ``seq`` per ``(site_id, sale_id)``, with ties broken on ``status``; apply
        that rule yourself when you need one row per sale.

        Performance: one site for a week is a few thousand rows in about a second.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 7 days.
            site: One site ID (required).
            statuses: ARM status code or codes to keep (see ``ARM_STATUS``), e.g. ``1``
                for joins or ``[120, 130]`` for declines and recharge problems. None keeps
                every status.

        Returns:
            Events ordered by paid time, sale ID and ``seq``.

        Raises:
            ValidationError: Invalid dates, site or statuses, or a range longer than 7 days.

        Examples:
            >>> joins = client.memberships.events("2026-10-05", "2026-10-11", 24, statuses=1)
        """
        site_id = normalize_site(site)
        start_dt, end_dt = normalize_date_range(start, end, RANGE_LIMITS["memberships.events"])
        status_list = _normalize_statuses(statuses)
        sql, params = events_sql(site_id, start_dt, end_dt, status_list)
        rows = _rows(self._client._execute(sql, params))
        if not rows:
            return []
        code = self._site_codes([site_id]).get(site_id, str(site_id))
        return [
            MembershipEvent(
                site_id=int(r["site"]),
                site_code=code,
                sale_id=int(r["saleid"]),
                seq=_int(r.get("seq")),
                log_date=_date(r["logdate"]),
                event_at=_datetime(r.get("eventat")),
                sale_status_code=_str(r.get("salestatuscode")),
                customer_id=_int(r.get("customer")),
                leader_id=_int(r.get("leader")),
                plan_type_id=_int(r.get("plantype")),
                plan_name=_str(r.get("planname")),
                status=int(r["status"]),
                status_name=_str(r.get("statusname")),
                washes=_int(r.get("washes")),
                expires=_date(r.get("expires")),
                family_members=_int(r.get("members")),
                amount=_money(r.get("amount")),
                upsell=_money(r.get("upsell")),
                first_sale_site_id=_int(r.get("firstsalesite")),
            )
            for r in rows
        ]
