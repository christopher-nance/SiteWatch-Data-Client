"""Pydantic result models for labor: employees, timecards and labor statistics."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sitewatch_data_client.types._base import SiteWatchModel


class PunchSource(str, Enum):
    """How a punch time came to be, decoded from ``ActualIn`` / ``ActualOut``.

    Attributes:
        TERMINAL: A real terminal punch (tenths of a minute that match the recorded minute).
        EDITED: Changed in the Timecard Editor (codes 30001/30002, or tenths that do not
            match the recorded minute).
        MISSING_OUT: No real clock-out (codes 30000/30003/30004); the hours are a placeholder.
        CONTINUATION: Day-change (-30000) or overtime split (-30001/-30002) continuation.
        GROUP: Group punch (-1 .. -28800).
        UNKNOWN: ``ActualIn`` / ``ActualOut`` is NULL.
        OTHER: Any other value.
    """

    TERMINAL = "terminal"
    EDITED = "edited"
    MISSING_OUT = "missing_out"
    CONTINUATION = "continuation"
    GROUP = "group"
    UNKNOWN = "unknown"
    OTHER = "other"


class TimecardFlag(str, Enum):
    """Review flags on a timecard interval.

    Attributes:
        OPEN: No real clock-out (``ActualOut`` 30000/30003/30004); fix in SiteWatch before
            paying. Only the fragment that carries the open code has this flag.
        OPEN_CHAIN: Part of a shift with no real clock-out: the OPEN fragment plus every
            earlier piece of the same shift joined to it by a midnight or overtime split
            (for example ``10:36-24:00`` on day 1 continued as ``00:00-02:36 OPEN`` on
            day 2). All of these hours are SiteWatch placeholders.
        EDITED: Either side was edited in the Timecard Editor.
        CROSSES_MIDNIGHT: The interval ends at midnight and continues the next day.
        SW_OVERTIME: SiteWatch classed the interval as overtime (PiType ``O``).
        OVERLAP: Clock-in is earlier than the same employee's latest earlier clock-out at
            any requested site (double-clocked).
    """

    OPEN = "OPEN"
    OPEN_CHAIN = "OPEN_CHAIN"
    EDITED = "EDITED"
    CROSSES_MIDNIGHT = "CROSSES_MIDNIGHT"
    SW_OVERTIME = "SW_OVERTIME"
    OVERLAP = "OVERLAP"


class Employee(SiteWatchModel):
    """An employee master record (safe columns only).

    Sensitive ``V_Employee`` columns (SSN, passwords, security code, driver's licence,
    address, phones, birthday, wage) are never selected.

    Attributes:
        employee_id: ``V_Employee.ObjID``, the internal key that punches join on.
        employee_number: ``V_Employee.Code``, the human / payroll employee number.
        first_name: First name as typed in SiteWatch.
        last_name: Last name.
        full_name: ``V_Employee.Name``.
        home_site_id: Home site ID. May be a legacy ``OLD-*`` site.
        category_id: ``V_Employee.Category``.
        category: Category name (Store Manager, Line Labor, Sales Labor, Admin, ...). None
            when the category is missing from ``V_EmpCategory``.
        hire_date: Hire date.
        term_date: Termination date.
        enabled: True when ``Enabled = 1`` (SiteWatch uses 2 for disabled).
    """

    employee_id: int
    employee_number: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    home_site_id: int | None = None
    category_id: int | None = None
    category: str | None = None
    hire_date: date | None = None
    term_date: date | None = None
    enabled: bool


class TimecardInterval(SiteWatchModel):
    """One ``V_PunchInterval`` row, decoded for payroll.

    Attributes:
        site_id: Worked site ID.
        site_code: Worked site code, e.g. ``ICONTN-030``.
        site_name: Worked site name.
        employee_id: ``V_Employee.ObjID``.
        employee_number: ``V_Employee.Code``.
        first_name: First name.
        last_name: Last name.
        full_name: Full name.
        home_site_id: Employee home site (may differ from the worked site).
        employee_category: Employee category name.
        labor_role_id: ``V_PunchInterval.Department`` (``V_EmpRole.ObjID``).
        labor_role: Labor role name used for the interval.
        punch_date: Business date of the interval.
        pay_week_start: Saturday that starts the Sat-Fri pay week.
        clock_in: ``PunchDate + InTime`` minutes (site-local wall clock).
        clock_out: ``PunchDate + OutTime`` minutes; ``OutTime`` 1440 is next-day 00:00.
        hours: ``(OutTime - InTime) / 60`` rounded to 4 dp, exactly as SiteWatch computes it.
        pi_type: ``R`` regular or ``O`` overtime.
        multiplier: Pay multiplier (1.0 / 1.5).
        in_source: How the clock-in time came to be.
        out_source: How the clock-out time came to be.
        new_shift: True for the first interval of an employee/site/date (lunch pairs and
            continuations are not new shifts).
        gap_before_min: Minutes off the clock since the previous clock-out the same day.
        flags: Review flags.
        actual_in_raw: Raw ``ActualIn`` (tenths of a minute or a code).
        actual_out_raw: Raw ``ActualOut``.
    """

    site_id: int
    site_code: str
    site_name: str
    employee_id: int
    employee_number: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    home_site_id: int | None = None
    employee_category: str | None = None
    labor_role_id: int | None = None
    labor_role: str | None = None
    punch_date: date
    pay_week_start: date
    clock_in: datetime
    clock_out: datetime
    hours: float
    pi_type: str
    multiplier: float | None = None
    in_source: PunchSource
    out_source: PunchSource
    new_shift: bool
    gap_before_min: int | None = None
    flags: list[TimecardFlag]
    actual_in_raw: int | None = None
    actual_out_raw: int | None = None


class WeeklyHours(SiteWatchModel):
    """An employee's hours in one Sat-Fri pay week across all requested sites.

    Attributes:
        pay_week_start: Saturday that starts the pay week.
        hours: Hours at every requested site combined, 2 dp (for a cross-site OT review; a
            week partly outside the window is only partly counted).
    """

    pay_week_start: date
    hours: float


class TimecardTotal(SiteWatchModel):
    """Per-employee, per-worked-site timecard totals.

    Attributes:
        site_id: Worked site ID.
        site_code: Worked site code.
        site_name: Worked site name.
        employee_id: ``V_Employee.ObjID``.
        employee_number: ``V_Employee.Code``.
        first_name: First name.
        last_name: Last name.
        home_site_id: Employee home site.
        employee_category: Employee category name.
        total_hours: All hours at this site, 2 dp.
        sw_regular_hours: PiType ``R`` hours, 2 dp.
        sw_overtime_hours: PiType ``O`` hours (SiteWatch's per-site weekly OT), 2 dp.
        shifts: Number of shifts (employee/site/date starts).
        intervals: Number of punch intervals.
        open_punches: Intervals flagged OPEN (one per shift with no real clock-out).
        open_hours: Placeholder hours on those shifts, 2 dp: every interval flagged
            OPEN_CHAIN, not only the open-coded fragment.
        edited_punches: Intervals flagged EDITED.
        overlap_punches: Intervals flagged OVERLAP.
        weekly_hours_all_sites: The employee's hours per pay week at every requested site,
            for each week the employee worked at this site.
    """

    site_id: int
    site_code: str
    site_name: str
    employee_id: int
    employee_number: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    home_site_id: int | None = None
    employee_category: str | None = None
    total_hours: float
    sw_regular_hours: float
    sw_overtime_hours: float
    shifts: int
    intervals: int
    open_punches: int
    open_hours: float
    edited_punches: int
    overlap_punches: int
    weekly_hours_all_sites: list[WeeklyHours]


class TimecardReport(SiteWatchModel):
    """Timecard intervals plus payroll totals and review warnings.

    Attributes:
        period_start: First punch date requested.
        period_end: Last punch date requested (inclusive).
        whole_pay_weeks: True when ``period_start`` is a Saturday and ``period_end`` a Friday.
        latest_punch_date: Latest punch date found, or None when there are no punches.
        intervals: Every interval, as returned by ``timecards()``.
        totals: Per-employee, per-site totals, ordered by site then name.
        warnings: Stale data, OPEN, OVERLAP, partial-week and DST review warnings.
    """

    period_start: date
    period_end: date
    whole_pay_weeks: bool
    latest_punch_date: date | None = None
    intervals: list[TimecardInterval]
    totals: list[TimecardTotal]
    warnings: list[str]


class LaborStat(SiteWatchModel):
    """One row of SiteWatch's ``Labor_Statistics`` procedure.

    Dimensions not in ``group_by`` are rolled up and come back as None.

    Attributes:
        site_id: Site the statistics are for.
        punch_date: Business date, when grouped by date.
        employee_id: ``V_Employee.ObjID``, when grouped by employee.
        employee_number: ``V_Employee.Code``.
        first_name: First name.
        last_name: Last name.
        role_id: ``V_EmpRole.ObjID``, when grouped by role.
        role: Role name.
        pi_type: ``R`` regular or ``O`` overtime.
        minutes: Clocked minutes.
        hours: ``minutes / 60``, 2 dp.
        dollars: Labor dollars, 2 dp. None unless ``include_dollars=True``.
    """

    site_id: int
    punch_date: date | None = None
    employee_id: int | None = None
    employee_number: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    role_id: int | None = None
    role: str | None = None
    pi_type: str | None = None
    minutes: float
    hours: float
    dollars: float | None = None


class DailyLaborHours(SiteWatchModel):
    """Clocked labor hours for one site and day (the CPLH denominator).

    Attributes:
        site_id: Site ID.
        site_code: Site code.
        punch_date: Business date.
        employees: Distinct employees who punched (an employee with R and O counts once).
        regular_hours: PiType ``R`` hours, 2 dp.
        overtime_hours: PiType ``O`` hours, 2 dp.
        total_hours: ``R`` plus ``O`` hours, 2 dp.
        open_punches: Intervals that day belonging to a shift with no real clock-out
            (flag OPEN_CHAIN): the open-coded fragment, and also a ``-24:00`` piece whose
            next-day continuation is open.
        open_hours: Placeholder hours on those intervals, 2 dp. Included in the hour
            totals.
        excluded_hours: Hours of employees left out by ``exclude_home_sites`` (office
            accounts by default), 2 dp. Not included in the hour totals.
        provisional: True when ``open_punches > 0``: the hours, and any ratio built on
            them, will change once the open punches are fixed in SiteWatch.
    """

    site_id: int
    site_code: str
    punch_date: date
    employees: int
    regular_hours: float
    overtime_hours: float
    total_hours: float
    open_punches: int
    open_hours: float = 0.0
    excluded_hours: float = 0.0
    provisional: bool = False


class DailyProductivity(SiteWatchModel):
    """Cars and revenue per clocked labor hour for one site and day.

    A Python merge of ``client.sales.daily_summary`` and ``client.labor.daily_hours`` on
    ``(site_id, date)``. A day with ``provisional=True`` (``open_punches > 0``) has
    placeholder hours from shifts with no real clock-out, so its ratios will change.

    Revenue per labor hour uses ``wash_revenue``: ARM auto-recharge revenue (terminal
    ``Automatic Recharge``, posted in a batch around 06:20 on billing days) is not driven
    by labor and swings by more than 10x with the billing calendar, so it is left out.

    Attributes:
        site_id: Site ID.
        site_code: Site code.
        log_date: Business date (``LogDate`` for sales, ``PunchDate`` for labor).
        car_count: SiteWatch car count (``TotalCarCount``), 0 if the day had no sales.
        revenue: ``DailySales.revenue``, 0.0 if the day had no sales. Includes ARM
            auto-recharge revenue.
        recharge_revenue: Completed sales total from the ``Automatic Recharge`` terminal.
        wash_revenue: ``revenue - recharge_revenue``.
        labor_hours: Clocked R plus O hours, 0.0 if nobody punched. Office accounts are
            excluded by default (see ``exclude_home_sites``).
        cars_per_labor_hour: ``car_count / labor_hours`` (2 dp), None when hours are 0.
        revenue_per_labor_hour: ``wash_revenue / labor_hours`` (2 dp), None when hours
            are 0.
        open_punches: Intervals that day on shifts with no real clock-out.
        open_hours: Placeholder hours inside ``labor_hours``.
        provisional: True when ``open_punches > 0``.
    """

    site_id: int
    site_code: str
    log_date: date
    car_count: int
    revenue: float
    labor_hours: float
    cars_per_labor_hour: float | None
    revenue_per_labor_hour: float | None
    open_punches: int
    recharge_revenue: float = 0.0
    wash_revenue: float = 0.0
    open_hours: float = 0.0
    provisional: bool = False
