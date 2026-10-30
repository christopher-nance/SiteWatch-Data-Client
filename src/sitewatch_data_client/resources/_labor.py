"""Labor resource: employees, payroll timecards and SiteWatch labor statistics.

The SQL builders (``*_sql``) and the pure timecard functions (``same_minute``,
``classify_actual``, ``week_start``, ``build_intervals``, ``build_totals``) are module-level
so they can be unit-tested without a database. The timecard logic is a behaviour-for-behaviour
port of the validated ``export_timecards.py`` payroll export.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sitewatch_data_client._constants import (
    OFFICE_HOME_SITE_IDS,
    PAY_WEEK_START_WEEKDAY,
    PUNCH_DAY_CHANGE_CODE,
    PUNCH_EDITED_CODES,
    PUNCH_OPEN_CODES,
    PUNCH_OT_SPLIT_CODES,
    PUNCH_TENTHS_MAX,
    RANGE_LIMITS,
)
from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client._filters import (
    DateInput,
    Sites,
    business_today,
    normalize_date_range,
    normalize_site,
    normalize_sites,
    placeholders,
)
from sitewatch_data_client.resources._sales import daily_auto_recharge_sql
from sitewatch_data_client.types._labor import (
    DailyLaborHours,
    DailyProductivity,
    Employee,
    LaborStat,
    PunchSource,
    TimecardFlag,
    TimecardInterval,
    TimecardReport,
    TimecardTotal,
    WeeklyHours,
)

if TYPE_CHECKING:
    from sitewatch_data_client._client import SiteWatchClient

logger = logging.getLogger(__name__)

#: Maximum number of IDs accepted by ``employees(employee_ids=...)``.
MAX_EMPLOYEE_IDS = 500
#: PiTypes returned by ``timecards`` by default (the only ones seen at ICON).
DEFAULT_PI_TYPES: tuple[str, ...] = ("R", "O")
#: ``labor_statistics`` grouping dimensions, in procedure argument order.
LABOR_STAT_DIMENSIONS: tuple[str, ...] = ("date", "employee", "role")

_CONTINUATION_CODES = frozenset({PUNCH_DAY_CHANGE_CODE, *PUNCH_OT_SPLIT_CODES})
_MINUTES_PER_DAY = 1440
_DST_CHANGE_MINUTE = 120  # 02:00 wall clock

# Explicit, safe V_Employee column list. Never add SSNUM, PWORD, SECURITYCODE, DRVLNUM, WEB*,
# address, phone, birthday, wage or rate columns.
_EMPLOYEE_COLUMNS = (
    "e.ObjID, e.Code, e.FirstName, e.LastName, e.Name, e.HomeSite, e.Category, "
    "c.Name AS CategoryName, e.HireDate, e.TermDate, e.Enabled"
)


# --- SQL builders -------------------------------------------------------------------------------
#
# Each builder returns ``(sql, params)``. Only library constants and ``?`` placeholder strings
# are formatted into the SQL text; every caller value is a parameter.


def employees_sql(
    sites: Sequence[int] | None,
    *,
    employee_ids: Sequence[int] | None,
    active_as_of: date | None,
) -> tuple[str, list[Any]]:
    """Build the employee-master statement.

    Args:
        sites: Home-site IDs to filter on, or None for no home-site filter.
        employee_ids: ``ObjID`` values to filter on, or None.
        active_as_of: When set, keep ``Enabled = 1`` records whose ``TermDate`` is NULL or
            after this date.

    Returns:
        ``(sql, params)``.
    """
    where: list[str] = []
    params: list[Any] = []
    if employee_ids is not None:
        where.append(f"e.ObjID IN ({placeholders(len(employee_ids))})")
        params.extend(employee_ids)
    if sites is not None:
        where.append(f"e.HomeSite IN ({placeholders(len(sites))})")
        params.extend(sites)
    if active_as_of is not None:
        where.append("e.Enabled = 1 AND (e.TermDate IS NULL OR e.TermDate > ?)")
        params.append(active_as_of)
    sql = (
        f"SELECT {_EMPLOYEE_COLUMNS}\n"
        "FROM V_Employee e\n"
        "LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category\n"
        + (f"WHERE {' AND '.join(where)}\n" if where else "")
        + "ORDER BY e.LastName, e.FirstName, e.ObjID"
    )
    return sql, params


def worked_employees_sql(
    sites: Sequence[int], start_dt: datetime, end_excl: datetime
) -> tuple[str, list[Any]]:
    """Build the "who punched at these sites" statement (derived table plus JOIN)."""
    sql = (
        f"SELECT {_EMPLOYEE_COLUMNS}\n"
        "FROM (SELECT p.Employee FROM V_PunchInterval p\n"
        f"      WHERE p.Site IN ({placeholders(len(sites))})"
        " AND p.PunchDate >= ? AND p.PunchDate < ?\n"
        "      GROUP BY p.Employee) w\n"
        "JOIN V_Employee e ON e.ObjID = w.Employee\n"
        "LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category\n"
        "ORDER BY e.LastName, e.FirstName, e.ObjID"
    )
    return sql, [*sites, start_dt, end_excl]


def timecards_sql(
    sites: Sequence[int],
    start_dt: datetime,
    end_excl: datetime,
    pi_types: Sequence[str],
) -> tuple[str, list[Any]]:
    """Build the timecard interval statement (port of ``export_timecards.py``).

    The ``ORDER BY`` is part of the contract: the OVERLAP and gap logic in
    :func:`build_intervals` relies on employee, date, InTime order. ``Rate`` is not selected.
    """
    sql = (
        "SELECT p.Site, s.Code AS SiteCode, s.SiteName,\n"
        "       p.Employee AS EmployeeID, e.Code AS EmployeeNumber, e.FirstName, e.LastName,"
        " e.Name AS FullName,\n"
        "       e.HomeSite, c.Name AS EmpCategory, p.Department AS LaborRoleID,"
        " r.Name AS LaborRole, p.PiType,\n"
        "       p.PunchDate, p.InTime, p.OutTime, p.ActualIn, p.ActualOut, p.Multiplier\n"
        "FROM V_PunchInterval p\n"
        "JOIN V_Site s ON s.ID = p.Site\n"
        "LEFT JOIN V_Employee e ON e.ObjID = p.Employee\n"
        "LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category\n"
        "LEFT JOIN V_EmpRole r ON r.ObjID = p.Department\n"
        f"WHERE p.Site IN ({placeholders(len(sites))})"
        " AND p.PunchDate >= ? AND p.PunchDate < ?"
        f" AND p.PiType IN ({placeholders(len(pi_types))})\n"
        "ORDER BY e.LastName, e.FirstName, p.Employee, p.PunchDate, p.InTime, p.Site"
    )
    return sql, [*sites, start_dt, end_excl, *pi_types]


def daily_hours_sql(
    sites: Sequence[int], start_dt: datetime, fetch_end: datetime
) -> tuple[str, list[Any]]:
    """Build the interval statement behind :meth:`LaborResource.daily_hours`.

    Returns one row per R/O interval with only the columns needed to aggregate hours,
    detect open shifts and exclude office accounts (``HomeSite``); no names or rates.
    ``fetch_end`` is exclusive and should be one day after the last reported date, so a
    ``-24:00`` piece on the last day can be matched with its next-day continuation. The
    ``ORDER BY`` (employee, date, InTime) is required by :func:`build_intervals`.
    """
    sql = (
        "SELECT p.Site, s.Code AS SiteCode, p.Employee AS EmployeeID, e.HomeSite, p.PiType,\n"
        "       p.PunchDate, p.InTime, p.OutTime, p.ActualIn, p.ActualOut\n"
        "FROM V_PunchInterval p\n"
        "JOIN V_Site s ON s.ID = p.Site\n"
        "LEFT JOIN V_Employee e ON e.ObjID = p.Employee\n"
        f"WHERE p.Site IN ({placeholders(len(sites))})"
        " AND p.PunchDate >= ? AND p.PunchDate < ? AND p.PiType IN ('R', 'O')\n"
        "ORDER BY p.Employee, p.PunchDate, p.InTime, p.Site"
    )
    return sql, [*sites, start_dt, fetch_end]


def labor_statistics_sql(
    from_date: datetime,
    thru_date: datetime,
    site: int,
    orders: Sequence[int | None],
    filters: Sequence[int | None],
    *,
    include_dollars: bool,
) -> tuple[str, list[Any]]:
    """Build the ``Labor_Statistics`` statement.

    Args:
        from_date: First date (midnight).
        thru_date: Last date, **inclusive** (the procedure's own convention).
        site: Site ID.
        orders: ``(DateOrder, EmployeeOrder, RoleOrder)``; at least one must be non-None.
        filters: ``(ProfitCenter, LaborRole, EmployeeCategory, EmployeeID)``.
        include_dollars: Select the procedure's ``Dollars`` column.
    """
    dollars = ", CAST(ls.Dollars AS NUMERIC(12,2)) AS Dollars" if include_dollars else ""
    sql = (
        "SELECT ls.PunchDate, ls.Employee, e.Code AS EmployeeNumber, e.FirstName, e.LastName,\n"
        f"       ls.Role, r.Name AS RoleName, ls.PIType, ls.Minutes{dollars}\n"
        "FROM Labor_Statistics(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ls\n"
        "LEFT JOIN V_Employee e ON e.ObjID = ls.Employee\n"
        "LEFT JOIN V_EmpRole r ON r.ObjID = ls.Role"
    )
    return sql, [from_date, thru_date, site, *orders, *filters]


# --- Value helpers ------------------------------------------------------------------------------


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)  # NaN / NaT
    except (TypeError, ValueError):
        return False


def _int(value: Any) -> int | None:
    if _is_missing(value):
        return None
    return int(value)


def _float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    return float(value)


def _str(value: Any) -> str | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> date | None:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).strip()).date()


def _round(value: float, ndigits: int) -> float:
    return round(value + 0.0, ndigits)


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None or len(df) == 0:
        return []
    return df.to_dict("records")


def _validate_id(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an int, got {type(value).__name__}.")
    return value


# --- Pure timecard logic (port of export_timecards.py) ------------------------------------------


def same_minute(tenths: int, minute: int) -> bool:
    """Return True if a raw punch in tenths of a minute belongs to the recorded minute.

    The tenths are rounded (a punch at 5:50:58 is stored as 3510) while the minute is
    truncated (350), so a value ending in 0 may sit one minute above the recorded minute.
    That rounding exception is not an edit.

    Examples:
        >>> same_minute(6489, 648)
        True
        >>> same_minute(3510, 350)
        True
        >>> same_minute(9814, 758)
        False
    """
    return tenths // 10 == minute or (tenths % 10 == 0 and tenths // 10 - 1 == minute)


def classify_actual(actual: int | None, minute: int) -> PunchSource:
    """Describe how a punch time came to be from its ``ActualIn``/``ActualOut`` value.

    Args:
        actual: Raw ``ActualIn`` or ``ActualOut`` (tenths of a minute or a special code).
        minute: The recorded ``InTime`` or ``OutTime`` (minutes after midnight).

    Returns:
        The decoded :class:`PunchSource`.
    """
    if actual is None:
        return PunchSource.UNKNOWN
    if actual in PUNCH_OPEN_CODES:
        return PunchSource.MISSING_OUT
    if actual in _CONTINUATION_CODES:
        return PunchSource.CONTINUATION
    if actual in PUNCH_EDITED_CODES:
        return PunchSource.EDITED
    if -PUNCH_TENTHS_MAX <= actual <= -1:
        return PunchSource.GROUP if same_minute(abs(actual), minute) else PunchSource.EDITED
    if 0 <= actual <= PUNCH_TENTHS_MAX:
        # A terminal punch stores the real time in tenths; after a Timecard Editor change the
        # field holds the time of day the edit was saved, so the minutes disagree.
        return PunchSource.TERMINAL if same_minute(actual, minute) else PunchSource.EDITED
    return PunchSource.OTHER


def week_start(day: date) -> date:
    """Return the Saturday that starts the SiteWatch pay week containing ``day``.

    Examples:
        >>> week_start(date(2026, 10, 9))
        datetime.date(2026, 10, 3)
    """
    return day - timedelta(days=(day.weekday() - PAY_WEEK_START_WEEKDAY) % 7)


def us_dst_change_dates(year: int) -> tuple[date, date]:
    """Return the US DST start (second Sunday of March) and end (first Sunday of November).

    Examples:
        >>> us_dst_change_dates(2026)
        (datetime.date(2026, 3, 8), datetime.date(2026, 11, 1))
    """
    march1 = date(year, 3, 1)
    start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7)
    nov1 = date(year, 11, 1)
    end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return start, end


def build_intervals(rows: Iterable[Mapping[str, Any]]) -> list[TimecardInterval]:
    """Turn raw ``V_PunchInterval`` rows into decoded timecard intervals.

    ``rows`` must be in the :func:`timecards_sql` order (employee, date, InTime), because the
    OVERLAP and gap checks look at the employee's previous intervals.

    Args:
        rows: Row mappings with the lower-case column names of :func:`timecards_sql`.

    Returns:
        One :class:`TimecardInterval` per row, in input order.
    """
    out: list[TimecardInterval] = []
    last_out: dict[int, datetime] = {}
    max_out: dict[int, datetime] = {}
    seen_days: set[tuple[int, int, date]] = set()
    for r in rows:
        punch_date = _date(r["punchdate"])
        assert punch_date is not None  # PunchDate is NOT NULL in SiteWatch
        day = datetime(punch_date.year, punch_date.month, punch_date.day)
        employee_id = int(r["employeeid"])
        site_id = int(r["site"])
        in_time = int(r["intime"])
        out_time = int(r["outtime"])
        actual_in = _int(r.get("actualin"))
        actual_out = _int(r.get("actualout"))
        pi_type = _str(r.get("pitype")) or ""
        cin = day + timedelta(minutes=in_time)
        cout = day + timedelta(minutes=out_time)
        in_src = classify_actual(actual_in, in_time)
        out_src = classify_actual(actual_out, out_time)

        flags: list[TimecardFlag] = []
        if out_src is PunchSource.MISSING_OUT:
            flags.append(TimecardFlag.OPEN)
        if PunchSource.EDITED in (in_src, out_src):
            flags.append(TimecardFlag.EDITED)
        if out_time >= _MINUTES_PER_DAY and actual_out == PUNCH_DAY_CHANGE_CODE:
            flags.append(TimecardFlag.CROSSES_MIDNIGHT)
        if pi_type == "O":
            flags.append(TimecardFlag.SW_OVERTIME)
        if cin < max_out.get(employee_id, cin):
            flags.append(TimecardFlag.OVERLAP)
        max_out[employee_id] = max(cout, max_out.get(employee_id, cout))

        prev = last_out.get(employee_id)
        gap: int | None = None
        if (
            prev is not None
            and in_src is not PunchSource.CONTINUATION
            and prev.date() == cin.date()
            and cin >= prev
        ):
            gap = int((cin - prev).total_seconds() // 60)
        last_out[employee_id] = cout

        day_key = (employee_id, site_id, punch_date)
        new_shift = not (in_src is PunchSource.CONTINUATION or day_key in seen_days)
        seen_days.add(day_key)

        out.append(
            TimecardInterval(
                site_id=site_id,
                site_code=_str(r.get("sitecode")) or "",
                site_name=_str(r.get("sitename")) or "",
                employee_id=employee_id,
                employee_number=_int(r.get("employeenumber")),
                first_name=_str(r.get("firstname")),
                last_name=_str(r.get("lastname")),
                full_name=_str(r.get("fullname")),
                home_site_id=_int(r.get("homesite")),
                employee_category=_str(r.get("empcategory")),
                labor_role_id=_int(r.get("laborroleid")),
                labor_role=_str(r.get("laborrole")),
                punch_date=punch_date,
                pay_week_start=week_start(punch_date),
                clock_in=cin,
                clock_out=cout,
                hours=_round((out_time - in_time) / 60.0, 4),
                pi_type=pi_type,
                multiplier=_float(r.get("multiplier")),
                in_source=in_src,
                out_source=out_src,
                new_shift=new_shift,
                gap_before_min=gap,
                flags=flags,
                actual_in_raw=actual_in,
                actual_out_raw=actual_out,
            )
        )
    mark_open_chains(out)
    return out


def mark_open_chains(intervals: Sequence[TimecardInterval]) -> None:
    """Flag every piece of a shift that has no real clock-out with ``OPEN_CHAIN``.

    SiteWatch splits a shift at midnight (``ActualOut`` -30000) and at the overtime
    boundary (-30001/-30002). When the clock-out is missing, only the last piece carries
    the open code, so the earlier pieces (for example ``10:36-24:00`` the day before) look
    like normal terminal punches although their hours are placeholders too. A piece is
    joined to the next one when it ends in a continuation code and the next interval of the
    same employee starts at the same moment.

    ``intervals`` must be in :func:`timecards_sql` order. The flags are added in place.
    """
    chain: list[TimecardInterval] = []
    prev: TimecardInterval | None = None
    for iv in intervals:
        linked = (
            prev is not None
            and prev.employee_id == iv.employee_id
            and prev.out_source is PunchSource.CONTINUATION
            and prev.clock_out == iv.clock_in
        )
        if not linked:
            chain = []
        chain.append(iv)
        if TimecardFlag.OPEN in iv.flags:
            for piece in chain:
                if TimecardFlag.OPEN_CHAIN not in piece.flags:
                    piece.flags.append(TimecardFlag.OPEN_CHAIN)
        prev = iv


def aggregate_daily_hours(
    intervals: Iterable[TimecardInterval],
    first_day: date,
    last_day: date,
    exclude_home_sites: Sequence[int] = (),
) -> list[DailyLaborHours]:
    """Aggregate decoded intervals into per-site, per-day labor hours.

    Args:
        intervals: Output of :func:`build_intervals` (may include a look-ahead day).
        first_day: First reported date (inclusive).
        last_day: Last reported date (inclusive); later intervals are only used to detect
            open shifts.
        exclude_home_sites: Home-site IDs whose employees are left out of the totals and
            reported in ``excluded_hours`` instead.

    Returns:
        Rows ordered by site and date. Days without punches are absent.
    """
    excluded = set(exclude_home_sites)
    acc: dict[tuple[int, date], dict[str, Any]] = {}
    for iv in intervals:
        if not first_day <= iv.punch_date <= last_day:
            continue
        row = acc.setdefault(
            (iv.site_id, iv.punch_date),
            {
                "site_code": iv.site_code,
                "employees": set(),
                "reg": 0.0,
                "ot": 0.0,
                "open": 0,
                "open_h": 0.0,
                "excl": 0.0,
            },
        )
        if iv.home_site_id is not None and iv.home_site_id in excluded:
            row["excl"] += iv.hours
            continue
        row["employees"].add(iv.employee_id)
        row["ot" if iv.pi_type == "O" else "reg"] += iv.hours
        if TimecardFlag.OPEN_CHAIN in iv.flags:
            row["open"] += 1
            row["open_h"] += iv.hours

    out: list[DailyLaborHours] = []
    for (site_id, day), row in sorted(acc.items()):
        out.append(
            DailyLaborHours(
                site_id=site_id,
                site_code=row["site_code"],
                punch_date=day,
                employees=len(row["employees"]),
                regular_hours=_round(row["reg"], 2),
                overtime_hours=_round(row["ot"], 2),
                total_hours=_round(row["reg"] + row["ot"], 2),
                open_punches=row["open"],
                open_hours=_round(row["open_h"], 2),
                excluded_hours=_round(row["excl"], 2),
                provisional=row["open"] > 0,
            )
        )
    return out


def build_totals(intervals: Iterable[TimecardInterval]) -> list[TimecardTotal]:
    """Aggregate intervals into per-employee, per-worked-site totals.

    ``weekly_hours_all_sites`` is the employee's total at every site in ``intervals`` for each
    Sat-Fri pay week the employee worked at that site. It is for an OT review only; no
    overtime pay is computed.

    Returns:
        Totals ordered by site, then last and first name (case-insensitive).
    """
    acc: dict[tuple[int, int], dict[str, Any]] = {}
    week_hours: dict[tuple[int, date], float] = defaultdict(float)
    for d in intervals:
        week_hours[(d.employee_id, d.pay_week_start)] += d.hours
        t = acc.setdefault(
            (d.employee_id, d.site_id),
            {
                "site_id": d.site_id,
                "site_code": d.site_code,
                "site_name": d.site_name,
                "employee_id": d.employee_id,
                "employee_number": d.employee_number,
                "first_name": d.first_name,
                "last_name": d.last_name,
                "home_site_id": d.home_site_id,
                "employee_category": d.employee_category,
                "total_hours": 0.0,
                "sw_regular_hours": 0.0,
                "sw_overtime_hours": 0.0,
                "shifts": 0,
                "intervals": 0,
                "open_punches": 0,
                "open_hours": 0.0,
                "edited_punches": 0,
                "overlap_punches": 0,
                "weeks": set(),
            },
        )
        t["total_hours"] += d.hours
        t["sw_overtime_hours" if d.pi_type == "O" else "sw_regular_hours"] += d.hours
        t["shifts"] += int(d.new_shift)
        t["intervals"] += 1
        if TimecardFlag.OPEN in d.flags:
            t["open_punches"] += 1
        if TimecardFlag.OPEN_CHAIN in d.flags:
            t["open_hours"] += d.hours
        if TimecardFlag.EDITED in d.flags:
            t["edited_punches"] += 1
        if TimecardFlag.OVERLAP in d.flags:
            t["overlap_punches"] += 1
        t["weeks"].add(d.pay_week_start)

    totals: list[TimecardTotal] = []
    for t in acc.values():
        weeks = sorted(t.pop("weeks"))
        t["weekly_hours_all_sites"] = [
            WeeklyHours(pay_week_start=w, hours=_round(week_hours[(t["employee_id"], w)], 2))
            for w in weeks
        ]
        for k in ("total_hours", "sw_regular_hours", "sw_overtime_hours", "open_hours"):
            t[k] = _round(t[k], 2)
        totals.append(TimecardTotal(**t))
    totals.sort(
        key=lambda t: (t.site_id, (t.last_name or "").upper(), (t.first_name or "").upper())
    )
    return totals


def build_warnings(
    intervals: Sequence[TimecardInterval], period_start: date, period_end: date
) -> list[str]:
    """Return the payroll review warnings for a timecard window.

    The same conditions ``export_timecards.py`` printed (stale data, OPEN, OVERLAP, partial
    pay weeks), plus one warning per interval on a US DST change date whose span covers
    02:00. Warnings identify intervals by site, employee ID and time, never by name.
    """
    warnings: list[str] = []
    latest = max((i.punch_date for i in intervals), default=None)
    if latest is None or latest < period_end:
        warnings.append(
            f"Latest punch found is {latest.isoformat() if latest else 'none'} "
            f"(requested through {period_end.isoformat()}); data after it is not in the "
            "central database yet."
        )
    open_count = sum(TimecardFlag.OPEN in i.flags for i in intervals)
    if open_count:
        open_hours = sum(i.hours for i in intervals if TimecardFlag.OPEN_CHAIN in i.flags)
        warnings.append(
            f"{open_count} OPEN punch(es) have no real clock-out; {open_hours:.2f} hours on "
            "those shifts (including earlier pieces split at midnight or at overtime) are "
            "SiteWatch placeholders. Fix them in SiteWatch before paying."
        )
    overlap_count = sum(TimecardFlag.OVERLAP in i.flags for i in intervals)
    if overlap_count:
        warnings.append(
            f"{overlap_count} OVERLAP punch(es) start before an earlier interval of the same "
            "employee has ended (double-clocked)."
        )
    if period_start != week_start(period_start) or period_end != week_start(period_end) + timedelta(
        days=6
    ):
        warnings.append(
            "Window is not whole Sat-Fri pay weeks; weekly_hours_all_sites covers only part "
            "of a week."
        )
    dst_cache: dict[int, tuple[date, date]] = {}
    for i in intervals:
        year = i.punch_date.year
        if year not in dst_cache:
            dst_cache[year] = us_dst_change_dates(year)
        if i.punch_date not in dst_cache[year]:
            continue
        midnight = datetime(year, i.punch_date.month, i.punch_date.day)
        start_min = int((i.clock_in - midnight).total_seconds() // 60)
        end_min = int((i.clock_out - midnight).total_seconds() // 60)
        if start_min < _DST_CHANGE_MINUTE <= end_min:
            warnings.append(
                f"DST: site {i.site_id} employee_id {i.employee_id} interval "
                f"{i.clock_in:%Y-%m-%d %H:%M}-{i.clock_out:%H:%M} spans 02:00 on a DST change "
                "date; wall-clock hours differ from elapsed time by 1 hour. Review before paying."
            )
    return warnings


# --- Row mappers --------------------------------------------------------------------------------


def _row_to_employee(r: Mapping[str, Any]) -> Employee:
    return Employee(
        employee_id=int(r["objid"]),
        employee_number=_int(r.get("code")),
        first_name=_str(r.get("firstname")),
        last_name=_str(r.get("lastname")),
        full_name=_str(r.get("name")),
        home_site_id=_int(r.get("homesite")),
        category_id=_int(r.get("category")),
        category=_str(r.get("categoryname")),
        hire_date=_date(r.get("hiredate")),
        term_date=_date(r.get("termdate")),
        enabled=_int(r.get("enabled")) == 1,
    )


def _normalize_pi_types(pi_types: Iterable[str] | str) -> list[str]:
    if isinstance(pi_types, str):
        pi_types = [pi_types]
    try:
        items = list(pi_types)
    except TypeError:
        raise ValidationError("pi_types must be an iterable of one-letter strings.") from None
    if not items:
        raise ValidationError("pi_types cannot be empty.")
    out: list[str] = []
    for p in items:
        if not isinstance(p, str) or len(p.strip()) != 1 or not p.strip().isalpha():
            raise ValidationError(
                f"Invalid PiType {p!r}: expected a one-letter code such as 'R' or 'O'."
            )
        code = p.strip().upper()
        if code not in out:
            out.append(code)
    return out


def _normalize_employee_ids(employee_ids: Iterable[int]) -> list[int]:
    if isinstance(employee_ids, (str, bytes)) or isinstance(employee_ids, int):
        employee_ids = [employee_ids]  # type: ignore[list-item]
    try:
        items = list(employee_ids)
    except TypeError:
        raise ValidationError("employee_ids must be an iterable of ints.") from None
    if not items:
        raise ValidationError("employee_ids cannot be empty.")
    for v in items:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValidationError(f"Invalid employee ID: expected int, got {type(v).__name__}.")
    ids = sorted(set(items))
    if len(ids) > MAX_EMPLOYEE_IDS:
        raise ValidationError(
            f"At most {MAX_EMPLOYEE_IDS} employee IDs per call (got {len(ids)}); split the list."
        )
    return ids


def _normalize_group_by(group_by: Iterable[str] | str) -> list[str]:
    if isinstance(group_by, str):
        group_by = [group_by]
    try:
        items = [str(g).strip().lower() for g in group_by]
    except TypeError:
        raise ValidationError(
            "group_by must be an iterable of 'date', 'employee', 'role'."
        ) from None
    if not items:
        raise ValidationError(
            "group_by needs at least one of 'date', 'employee', 'role': Labor_Statistics "
            "fails when every Order argument is NULL."
        )
    for g in items:
        if g not in LABOR_STAT_DIMENSIONS:
            raise ValidationError(
                f"Invalid group_by value {g!r}: expected 'date', 'employee' or 'role'."
            )
    if len(set(items)) != len(items):
        raise ValidationError("group_by values must not repeat.")
    return items


class LaborResource:
    """Employees, payroll timecards and SiteWatch labor statistics.

    Access via ``client.labor``.

    Timecards come from ``V_PunchInterval``: one row per clocked interval, with ``InTime`` and
    ``OutTime`` in minutes after midnight of ``PunchDate`` (wall-clock, no rounding). Pay weeks
    run Saturday to Friday, and SiteWatch applies 40-hour weekly OT per site. Pay rates are
    never returned except ``labor_statistics(include_dollars=True)``.
    """

    def __init__(self, client: SiteWatchClient) -> None:
        self._client = client

    def employees(
        self,
        sites: Sites = None,
        *,
        active_only: bool = True,
        employee_ids: Iterable[int] | None = None,
    ) -> list[Employee]:
        """List employee master records by home site.

        Selects an explicit, safe column list only: SSN, passwords, security code, driver's
        licence, web login, address, phones, birthday and wage are never read.

        ``active_only`` keeps ``Enabled = 1`` records whose ``TermDate`` is empty or after
        today (the US Central business date, computed in Python). ``Enabled`` alone is not
        enough: many enabled records are past their TermDate. Even the combined filter
        misses roughly 1 in 60 people who are still punching, and it never finds people who
        float in from another home site. Use :meth:`worked_employees` for the authoritative
        "who works here" list.

        Args:
            sites: Home-site IDs. None means the ICON car-wash sites (5, 13, 18, 20, 24, 25).
                When ``employee_ids`` is given and ``sites`` is None, no home-site filter is
                applied.
            active_only: Keep only active employees (see above). Defaults to True.
            employee_ids: Look up these ``V_Employee.ObjID`` values (at most 500).

        Returns:
            Employees ordered by last name, first name and ID.

        Raises:
            ValidationError: If a site or employee ID is invalid.

        Examples:
            >>> staff = client.labor.employees(24)
            >>> managers = [e for e in staff if e.category == "Store Manager"]
            >>> one = client.labor.employees(employee_ids=[99999], active_only=False)
        """
        ids = _normalize_employee_ids(employee_ids) if employee_ids is not None else None
        if ids is not None and sites is None:
            site_list = None
        else:
            site_list = normalize_sites(sites)
        sql, params = employees_sql(
            site_list,
            employee_ids=ids,
            active_as_of=business_today() if active_only else None,
        )
        df = self._client._execute(sql, params)
        return [_row_to_employee(r) for r in _records(df)]

    def worked_employees(
        self, start: DateInput, end: DateInput, sites: Sites = None
    ) -> list[Employee]:
        """List every employee who punched at ``sites`` in the window.

        Whatever their home site, Enabled flag or TermDate, so it includes floaters from
        legacy home sites. This is the authoritative "who works at these sites" list. One
        statement (derived table plus JOIN), about 0.2 s for six sites over two weeks.

        Args:
            start: First punch date (inclusive).
            end: Last punch date (inclusive). The range may span at most 93 days.
            sites: Worked-site IDs. None means the ICON car-wash sites.

        Returns:
            Employees ordered by last name, first name and ID.

        Raises:
            ValidationError: If the dates, range or sites are invalid.

        Examples:
            >>> crew = client.labor.worked_employees("2026-10-01", "2026-10-14", sites=24)
        """
        start_dt, end_excl = normalize_date_range(
            start, end, RANGE_LIMITS["labor.worked_employees"]
        )
        sql, params = worked_employees_sql(normalize_sites(sites), start_dt, end_excl)
        df = self._client._execute(sql, params)
        return [_row_to_employee(r) for r in _records(df)]

    def timecards(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
        *,
        pi_types: Iterable[str] | str = DEFAULT_PI_TYPES,
    ) -> list[TimecardInterval]:
        """Fetch payroll timecard intervals with names, sources and review flags.

        A port of the validated ``export_timecards.py`` export: one row per
        ``V_PunchInterval`` interval with clock-in/out timestamps, hours exactly as SiteWatch
        computes them, the Sat-Fri ``pay_week_start``, how each punch time came to be
        (terminal, edited, missing_out, continuation, group), ``new_shift``,
        ``gap_before_min`` and the flags OPEN, EDITED, CROSSES_MIDNIGHT, SW_OVERTIME and
        OVERLAP. A raw tenths value ending in 0 that sits one minute above the recorded
        minute is the rounding exception, not an edit. Rates are not selected.

        OVERLAP and the cross-site gap only see the requested sites, so narrowing ``sites``
        can hide a double-clock or OT at another site. Timestamps are site-local wall-clock
        time; on DST change nights an interval spanning 02:00 is 1 hour off from elapsed time
        (:meth:`timecard_totals` warns about these). One statement; two weeks for six sites
        returns in well under a second.

        Args:
            start: First punch date (inclusive).
            end: Last punch date (inclusive). The range may span at most 31 days.
            sites: Worked-site IDs. None means the ICON car-wash sites.
            pi_types: PiTypes to include. Defaults to ``("R", "O")``, the only ones used at
                ICON (``A`` adjustments and ``N`` non-work are excluded, as in the export).

        Returns:
            Intervals ordered by employee name, employee ID, date, clock-in and site.

        Raises:
            ValidationError: If the dates, range, sites or PiTypes are invalid.

        Examples:
            >>> cards = client.labor.timecards("2026-10-03", "2026-10-09", sites=24)
            >>> hours = sum(c.hours for c in cards)
            >>> edited = [c for c in cards if TimecardFlag.EDITED in c.flags]
        """
        start_dt, end_excl = normalize_date_range(start, end, RANGE_LIMITS["labor.timecards"])
        # One look-ahead day so a -24:00 piece on the last day is matched with its next-day
        # continuation (OPEN_CHAIN); the extra day is dropped. build_intervals only looks
        # back, so the trailing rows do not change the flags of earlier intervals.
        sql, params = timecards_sql(
            normalize_sites(sites),
            start_dt,
            end_excl + timedelta(days=1),
            _normalize_pi_types(pi_types),
        )
        df = self._client._execute(sql, params)
        last_day = (end_excl - timedelta(days=1)).date()
        return [i for i in build_intervals(_records(df)) if i.punch_date <= last_day]

    def timecard_totals(
        self, start: DateInput, end: DateInput, sites: Sites = None
    ) -> TimecardReport:
        """Fetch timecards and aggregate them into per-employee, per-site payroll totals.

        Calls :meth:`timecards` once (one statement) and aggregates in Python. The report
        carries both the intervals and the totals, so there is no need to query twice.
        Totals include regular and SiteWatch-OT hours, shifts, OPEN/EDITED/OVERLAP counts and
        the employee's weekly hours across every requested site per pay week. No overtime pay
        is computed.

        Warnings (also logged at WARNING) cover: data ending before ``end``, OPEN punches,
        OVERLAP punches, a window that is not whole Sat-Fri pay weeks, and intervals spanning
        02:00 on a US DST change date.

        Args:
            start: First punch date (inclusive). Use a Saturday for whole pay weeks.
            end: Last punch date (inclusive); the range may span at most 31 days. Use a Friday.
            sites: Worked-site IDs. None means the ICON car-wash sites.

        Returns:
            A :class:`TimecardReport`.

        Raises:
            ValidationError: If the dates, range or sites are invalid.

        Examples:
            >>> report = client.labor.timecard_totals("2026-09-26", "2026-10-09")
            >>> for w in report.warnings:
            ...     print(w)
            >>> for t in report.totals:
            ...     print(t.site_code, t.employee_number, t.total_hours, t.sw_overtime_hours)
        """
        start_dt, end_excl = normalize_date_range(start, end, RANGE_LIMITS["labor.timecard_totals"])
        period_start = start_dt.date()
        period_end = (end_excl - timedelta(days=1)).date()
        intervals = self.timecards(period_start, period_end, sites)
        totals = build_totals(intervals)
        warnings = build_warnings(intervals, period_start, period_end)
        for w in warnings:
            logger.warning(w)
        return TimecardReport(
            period_start=period_start,
            period_end=period_end,
            whole_pay_weeks=(
                period_start == week_start(period_start)
                and period_end == week_start(period_end) + timedelta(days=6)
            ),
            latest_punch_date=max((i.punch_date for i in intervals), default=None),
            intervals=intervals,
            totals=totals,
            warnings=warnings,
        )

    def labor_statistics(
        self,
        start: DateInput,
        end: DateInput,
        site: int,
        *,
        group_by: Iterable[str] | str = ("employee",),
        profit_center: int | None = None,
        labor_role: int | None = None,
        employee_category: int | None = None,
        employee_id: int | None = None,
        include_dollars: bool = False,
    ) -> list[LaborStat]:
        """Run SiteWatch's ``Labor_Statistics`` procedure for one site.

        Minutes (and hours) by PiType, grouped by any ordered mix of date, employee and role,
        like SiteWatch's Labor Statistics report. Its main use is cross-checking
        :meth:`timecards`: grouped by employee it equals the timecard minutes per employee and
        PiType (verified at all six ICON sites). ``end`` is passed as the procedure's
        inclusive ``ThruDate``. Dollars come from pay rates and are returned only when
        ``include_dollars=True``.

        Args:
            start: First date (inclusive).
            end: Last date (inclusive). The range may span at most 31 days.
            site: Site ID (required; the procedure covers one site).
            group_by: Ordered, non-empty subset of ``"date"``, ``"employee"``, ``"role"``.
                Positions become the procedure's DateOrder / EmployeeOrder / RoleOrder
                (1-3); dimensions left out are rolled up and come back as None.
            profit_center: Optional ProfitCenter filter.
            labor_role: Optional ``V_EmpRole.ObjID`` filter.
            employee_category: Optional ``V_EmpCategory.ObjID`` filter.
            employee_id: Optional ``V_Employee.ObjID`` filter.
            include_dollars: Also return the procedure's labor dollars.

        Returns:
            Rows ordered by date, last name, first name, employee ID, role and PiType.

        Raises:
            ValidationError: If the dates, range, site, ``group_by`` or a filter is invalid.
                An empty ``group_by`` is rejected because all-NULL order arguments crash
                the procedure.

        Examples:
            >>> stats = client.labor.labor_statistics("2026-10-03", "2026-10-09", 24)
            >>> sum(s.minutes for s in stats if s.pi_type == "R")
            >>> by_day = client.labor.labor_statistics(
            ...     "2026-10-03", "2026-10-09", 24, group_by=("date", "role")
            ... )
        """
        start_dt, end_excl = normalize_date_range(
            start, end, RANGE_LIMITS["labor.labor_statistics"]
        )
        site_id = normalize_site(site)
        dims = _normalize_group_by(group_by)
        orders = [dims.index(d) + 1 if d in dims else None for d in LABOR_STAT_DIMENSIONS]
        filters = [
            _validate_id(profit_center, "profit_center"),
            _validate_id(labor_role, "labor_role"),
            _validate_id(employee_category, "employee_category"),
            _validate_id(employee_id, "employee_id"),
        ]
        sql, params = labor_statistics_sql(
            start_dt,
            end_excl - timedelta(days=1),
            site_id,
            orders,
            filters,
            include_dollars=include_dollars,
        )
        df = self._client._execute(sql, params)
        stats: list[LaborStat] = []
        for r in _records(df):
            minutes = _float(r.get("minutes")) or 0.0
            dollars = _float(r.get("dollars")) if include_dollars else None
            stats.append(
                LaborStat(
                    site_id=site_id,
                    punch_date=_date(r.get("punchdate")),
                    employee_id=_int(r.get("employee")),
                    employee_number=_int(r.get("employeenumber")),
                    first_name=_str(r.get("firstname")),
                    last_name=_str(r.get("lastname")),
                    role_id=_int(r.get("role")),
                    role=_str(r.get("rolename")),
                    pi_type=_str(r.get("pitype")),
                    minutes=minutes,
                    hours=_round(minutes / 60.0, 2),
                    dollars=_round(dollars, 2) if dollars is not None else None,
                )
            )
        stats.sort(
            key=lambda s: (
                s.punch_date or date.min,
                (s.last_name or "").upper(),
                (s.first_name or "").upper(),
                s.employee_id if s.employee_id is not None else -1,
                s.role_id if s.role_id is not None else -1,
                s.pi_type or "",
            )
        )
        return stats

    def daily_hours(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
        *,
        exclude_home_sites: Iterable[int] | None = OFFICE_HOME_SITE_IDS,
    ) -> list[DailyLaborHours]:
        """Clocked labor hours per site per day, split by PiType.

        The denominator for cars per labor hour (CPLH). Employees are counted once per
        site-day even with both R and O intervals.

        Office accounts (home site 99) are excluded by default: they clock in at car-wash
        sites, rarely clock out, and produce most of the open punches, so their placeholder
        hours would swamp the crew hours. Their hours are reported in ``excluded_hours``.

        A shift with no real clock-out carries placeholder hours. SiteWatch puts the open
        code only on the last piece of a split shift, so the earlier pieces (for example the
        ``-24:00`` part the day before) are matched to it here; those intervals count in
        ``open_punches`` and ``open_hours``, and the day is ``provisional``. One statement
        reads the window plus one look-ahead day.

        Args:
            start: First punch date (inclusive).
            end: Last punch date (inclusive). The range may span at most 93 days.
            sites: Worked-site IDs. None means the ICON car-wash sites.
            exclude_home_sites: Home-site IDs whose employees are left out. Defaults to the
                office home site ``(99,)``; pass None or ``()`` to count everyone.

        Returns:
            Rows ordered by site and date. Days without punches are absent.

        Raises:
            ValidationError: If the dates, range, sites or ``exclude_home_sites`` are
                invalid.

        Examples:
            >>> days = client.labor.daily_hours("2026-10-01", "2026-10-14", sites=[5, 24])
            >>> sum(d.total_hours for d in days)
            >>> shaky = [d for d in days if d.provisional]
        """
        start_dt, end_excl = normalize_date_range(start, end, RANGE_LIMITS["labor.daily_hours"])
        excluded: list[int] = []
        if exclude_home_sites is not None and not (
            isinstance(exclude_home_sites, (tuple, list, set, frozenset))
            and len(exclude_home_sites) == 0
        ):
            excluded = normalize_sites(exclude_home_sites)
        sql, params = daily_hours_sql(
            normalize_sites(sites), start_dt, end_excl + timedelta(days=1)
        )
        df = self._client._execute(sql, params)
        return aggregate_daily_hours(
            build_intervals(_records(df)),
            start_dt.date(),
            (end_excl - timedelta(days=1)).date(),
            excluded,
        )

    def productivity(
        self,
        start: DateInput,
        end: DateInput,
        sites: Sites = None,
        *,
        exclude_home_sites: Iterable[int] | None = OFFICE_HOME_SITE_IDS,
    ) -> list[DailyProductivity]:
        """Cars and revenue per clocked labor hour (CPLH) per site per day.

        A Python merge of ``client.sales.daily_summary``, :meth:`daily_hours` and one small
        statement for ARM auto-recharge revenue, on ``(site, date)``. The statements run one
        after the other. A site-day that has sales but no punches (or punches but no sales)
        is still returned, with zeros on the missing side and ``None`` ratios when there are
        no hours.

        ``revenue_per_labor_hour`` uses ``wash_revenue``: revenue minus the completed sales
        of the ``Automatic Recharge`` terminal. ARM recharges are booked in a batch around
        06:20 on billing days (over $11,000 at one site on one day), are not driven by
        labor, and would make the ratio swing by more than 10x. ``revenue`` still includes
        them.

        Office accounts (home site 99) are left out of the hours by default, and a day with
        placeholder hours from a shift with no real clock-out is ``provisional``; see
        :meth:`daily_hours`.

        Args:
            start: First business date (inclusive).
            end: Last business date (inclusive). The range may span at most 31 days.
            sites: Site IDs. None means the ICON car-wash sites.
            exclude_home_sites: Passed to :meth:`daily_hours`. Defaults to ``(99,)``.

        Returns:
            Rows ordered by site and date.

        Raises:
            ValidationError: If the dates, range or sites are invalid.

        Examples:
            >>> rows = client.labor.productivity("2026-10-05", "2026-10-11", sites=24)
            >>> [(r.log_date, r.cars_per_labor_hour) for r in rows]
        """
        start_dt, end_excl = normalize_date_range(start, end, RANGE_LIMITS["labor.productivity"])
        site_ids = normalize_sites(sites)
        sales = self._client.sales.daily_summary(start, end, sites=site_ids)
        hours = self.daily_hours(start, end, sites=site_ids, exclude_home_sites=exclude_home_sites)
        sql, params = daily_auto_recharge_sql(site_ids, start_dt, end_excl)
        recharges = _records(self._client._execute(sql, params))

        def new_row(code: str) -> dict[str, Any]:
            return {
                "site_code": code,
                "cars": 0,
                "revenue": 0.0,
                "recharge": 0.0,
                "hours": 0.0,
                "open": 0,
                "open_h": 0.0,
            }

        merged: dict[tuple[int, date], dict[str, Any]] = {}
        for s in sales:
            row = merged.setdefault((s.site_id, s.log_date), new_row(s.site_code))
            row["cars"] += s.car_count
            row["revenue"] += s.revenue
            row["site_code"] = row["site_code"] or s.site_code
        for r in recharges:
            day = _date(r.get("logdate"))
            if day is None:
                continue
            row = merged.setdefault((int(r["site"]), day), new_row(""))
            row["recharge"] += _float(r.get("rechargetotal")) or 0.0
        for h in hours:
            row = merged.setdefault((h.site_id, h.punch_date), new_row(h.site_code))
            row["hours"] += h.total_hours
            row["open"] += h.open_punches
            row["open_h"] += h.open_hours
            row["site_code"] = row["site_code"] or h.site_code

        out: list[DailyProductivity] = []
        for (site_id, day), row in sorted(merged.items()):
            hrs = _round(row["hours"], 2)
            wash = row["revenue"] - row["recharge"]
            out.append(
                DailyProductivity(
                    site_id=site_id,
                    site_code=row["site_code"] or "",
                    log_date=day,
                    car_count=int(row["cars"]),
                    revenue=_round(row["revenue"], 2),
                    recharge_revenue=_round(row["recharge"], 2),
                    wash_revenue=_round(wash, 2),
                    labor_hours=hrs,
                    cars_per_labor_hour=_round(row["cars"] / hrs, 2) if hrs > 0 else None,
                    revenue_per_labor_hour=_round(wash / hrs, 2) if hrs > 0 else None,
                    open_punches=int(row["open"]),
                    open_hours=_round(row["open_h"], 2),
                    provisional=row["open"] > 0,
                )
            )
        return out


__all__ = [
    "LaborResource",
    "aggregate_daily_hours",
    "build_intervals",
    "build_totals",
    "build_warnings",
    "classify_actual",
    "mark_open_chains",
    "same_minute",
    "us_dst_change_dates",
    "week_start",
]
