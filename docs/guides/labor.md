# Labor and Timecards

`client.labor` reads employees, payroll timecards and SiteWatch's own labor statistics.
Every method is read-only, every history method needs a bounded date range, and `sites=None`
means the ICON car-wash sites `(5, 13, 18, 20, 24, 25)`.

| Method | Returns | Range limit |
|---|---|---|
| `employees(sites=None, *, active_only=True, employee_ids=None)` | `list[Employee]` | none (master data) |
| `worked_employees(start, end, sites=None)` | `list[Employee]` | 93 days |
| `timecards(start, end, sites=None, *, pi_types=("R", "O"))` | `list[TimecardInterval]` | 31 days |
| `timecard_totals(start, end, sites=None)` | `TimecardReport` | 31 days |
| `labor_statistics(start, end, site, *, group_by=("employee",), ...)` | `list[LaborStat]` | 31 days |
| `daily_hours(start, end, sites=None, *, exclude_home_sites=(99,))` | `list[DailyLaborHours]` | 93 days |
| `productivity(start, end, sites=None, *, exclude_home_sites=(99,))` | `list[DailyProductivity]` | 31 days |

## Quick start

```python
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()

with SiteWatchClient() as client:
    report = client.labor.timecard_totals("2026-09-26", "2026-10-09")

    for warning in report.warnings:
        print("WARNING:", warning)

    for t in report.totals:
        print(f"{t.site_code}  #{t.employee_number}  {t.total_hours:6.2f} h  "
              f"OT {t.sw_overtime_hours:5.2f} h  open {t.open_punches}")
    # ICONTN-030  #99999   41.25 h  OT  1.25 h  open 0
```

`timecard_totals` runs **one** statement. The report carries the intervals as well as the totals,
so there is no need to call `timecards()` again.

## Where the numbers come from

Timecards come from `V_PunchInterval`, which has one row per clocked interval (not one row per raw punch).

- `InTime` and `OutTime` are **minutes after midnight** of `PunchDate`. `OutTime` can be 1440,
  meaning midnight at the end of the day.
- `clock_in = PunchDate + InTime` and `clock_out = PunchDate + OutTime`.
- `hours = (OutTime - InTime) / 60`. No rounding is configured in SiteWatch.
- Only PiType `R` (regular) and `O` (overtime) occur at ICON. `A` and `N` are excluded by default.
- Pay rates are never selected. Only `labor_statistics(include_dollars=True)` returns dollars.

The library's timecards were validated against the payroll export for 2026-09-26..10-09. Both
gave 583 intervals, 2,814.83 hours and 111 EDITED intervals, and they matched row for row. They
also equal `Labor_Statistics` per employee and PiType.

## Punch sources

`ActualIn` / `ActualOut` hold the raw terminal punch in tenths of a minute, or a special code.
Each interval decodes them into `in_source` and `out_source`:

| Raw value | `PunchSource` | Meaning |
|---|---|---|
| 0..28800 matching the recorded minute | `terminal` | Real terminal punch |
| 0..28800 **not** matching | `edited` | Changed in the Timecard Editor (the field holds the time the edit was saved) |
| 30001, 30002 | `edited` | Created / edited in the Timecard Editor |
| 30000, 30003, 30004 | `missing_out` | No real clock-out |
| -30000 | `continuation` | Day-change continuation (shift crossed midnight) |
| -30001, -30002 | `continuation` | Split where weekly overtime starts |
| -1..-28800 | `group` | Group punch |
| NULL | `unknown` | |

!!! note "The rounding exception"
    The tenths are **rounded** but the minute is **truncated**. A punch at 5:50:58 is stored
    as `3510` while `InTime` is `350`. A tenths value ending in 0 that is exactly one minute
    above the recorded minute is therefore *not* an edit. Without this rule, EDITED is
    over-counted (169 instead of 111 in the validation window).

## Flags

| Flag | When |
|---|---|
| `OPEN` | `out_source == missing_out`. The hours are SiteWatch's placeholder; fix the punch before paying. |
| `OPEN_CHAIN` | The interval belongs to a shift with no real clock-out: the `OPEN` fragment and every earlier piece of the same shift joined to it by a midnight (`-30000`) or overtime (`-30001`/`-30002`) split. All of these hours are placeholders. |
| `EDITED` | Either side is `edited`. |
| `CROSSES_MIDNIGHT` | `OutTime >= 1440` and `ActualOut == -30000`. |
| `SW_OVERTIME` | PiType `O`. SiteWatch applies 40-hour weekly OT **per site**. |
| `OVERLAP` | Clock-in is earlier than the same employee's latest earlier clock-out at any requested site (double-clocked). |

```python
from sitewatch_data_client.types._labor import TimecardFlag

cards = client.labor.timecards("2026-10-03", "2026-10-09", sites=24)
to_review = [c for c in cards if {TimecardFlag.OPEN, TimecardFlag.OVERLAP} & set(c.flags)]
```

!!! warning "Only the last piece of an open shift carries the open code"
    A shift that starts at 10:36 and is never clocked out is stored as `10:36-24:00` (ending in
    the day-change code, so it looks like a normal terminal punch) plus `00:00-02:36` the next
    day with `ActualOut = 30000`. Only the second piece is `OPEN`; both are `OPEN_CHAIN`.
    `TimecardTotal.open_hours` adds up every `OPEN_CHAIN` interval, so it is the whole
    placeholder time payroll must fix, while `open_punches` counts the missing clock-outs.
    `timecards()` reads one extra day past `end` to see the continuation of a shift that crosses
    the last midnight, and drops that day from the result.

Each interval also carries:

- `new_shift`: True for the first interval of an employee, site and date. A lunch
  punch-out/in pair on the same day, and a continuation after midnight or into overtime,
  stay part of the same shift.
- `gap_before_min`: minutes off the clock since the previous clock-out on the same day.
  SiteWatch has no break rows, so a short gap is the closest thing to a recorded lunch.

## Pay weeks

Pay weeks run **Saturday to Friday**. `pay_week_start` is the Saturday. Ask for whole weeks
(start on a Saturday, end on a Friday) when running payroll; otherwise `whole_pay_weeks` is
False and the report warns that `weekly_hours_all_sites` covers only part of a week.

`TimecardTotal.weekly_hours_all_sites` lists, for each week the employee worked at that site,
their hours at **every requested site** combined. It exists to spot cross-site overtime. The
library does not compute overtime pay; see the open payroll questions in the schema map
(`docs/schema/employees-labor.md` in the ODBC tester repo).

!!! warning "Cross-site checks only see the requested sites"
    OVERLAP and `weekly_hours_all_sites` look only at the sites you pass. Narrowing `sites`
    to one location can hide a double-clock or overtime at another one. Use the default
    (all ICON car washes) for payroll.

## Warnings

`TimecardReport.warnings` holds the review warnings, and each one is also logged at WARNING:

- the latest punch found is before `end` (the central database lags the sites);
- OPEN punches exist (with the placeholder hours on their whole shifts);
- OVERLAP punches exist;
- the window is not whole Sat-Fri pay weeks;
- an interval on a US DST change date spans 02:00.

Warnings identify intervals by site, employee ID and time, never by name.

### Daylight saving

`InTime` and `OutTime` are wall-clock minutes with no UTC offset. On a DST change night, an
interval that spans 02:00 is off from elapsed time by 1 hour: 1 hour short in November and
1 hour long in March. `Labor_Statistics` uses the same arithmetic, so the library keeps SiteWatch's hours
unchanged and adds a warning so payroll can review the interval. 2026-11-01 is the next change date.

## Employees: active vs worked

```python
staff = client.labor.employees(24)                       # active, home site 24
everyone = client.labor.employees(24, active_only=False) # include disabled / terminated
one = client.labor.employees(employee_ids=[99999], active_only=False)
crew = client.labor.worked_employees("2026-10-01", "2026-10-14")
```

`employees()` filters on **home site**. `active_only` keeps `Enabled = 1` records whose
`TermDate` is empty or after today (the US Central business date, computed in Python, so a UTC
host does not shift it).

- `Enabled` alone is not enough. Many enabled records are past their TermDate.
- Even the combined filter misses about 1 in 60 people who are still punching.
- It never finds floaters whose home site is a legacy `OLD-*` site.

For "who actually works here", use `worked_employees()`. It returns every employee with a punch
at the sites in the window, whatever their home site, Enabled flag or TermDate. For the six
ICON sites over 2026-10-01..14 that was 43 people, 3 of them with a legacy home site.

Only safe columns are read. SSN, passwords, security code, driver's licence, web login,
address, phones, birthday and wage are never selected.

| Field | Source |
|---|---|
| `employee_id` | `V_Employee.ObjID`, the key that punches join on |
| `employee_number` | `V_Employee.Code`, the human / payroll number |
| `home_site_id` | May be a legacy site |
| `category` | Store Manager, Line Labor, Sales Labor, Admin, ... (None if the category row is missing) |
| `enabled` | `Enabled = 1` (SiteWatch uses 2 for disabled) |

## Labor statistics (cross-check)

`labor_statistics` wraps SiteWatch's `Labor_Statistics` procedure for **one site**, the same
numbers as the Labor Statistics report. Its main use is checking a timecard export.

```python
from collections import defaultdict

stats = client.labor.labor_statistics("2026-10-03", "2026-10-09", 24)

check = defaultdict(float)
for c in client.labor.timecards("2026-10-03", "2026-10-09", 24):
    check[(c.employee_id, c.pi_type)] += c.hours * 60

assert {k: round(v) for k, v in check.items()} == {
    (s.employee_id, s.pi_type): s.minutes for s in stats
}
```

- `group_by` is an ordered, non-empty mix of `"date"`, `"employee"` and `"role"`. Dimensions left
  out are rolled up and come back as None. An empty `group_by` is rejected, because the
  procedure fails when every Order argument is NULL.
- `end` is passed as the procedure's **inclusive** `ThruDate`.
- Optional filters: `profit_center`, `labor_role` (`V_EmpRole.ObjID`), `employee_category`
  and `employee_id`.
- `dollars` is None unless you pass `include_dollars=True`.

```python
by_day_and_role = client.labor.labor_statistics(
    "2026-10-03", "2026-10-09", 24, group_by=("date", "role")
)
for s in by_day_and_role:
    print(s.punch_date, s.role, s.pi_type, s.hours)
# 2026-10-03 Line Labor R 18.5
```

## Daily hours (CPLH denominator)

```python
days = client.labor.daily_hours("2026-10-01", "2026-10-14")
for d in days:
    flag = f" (provisional: {d.open_hours} placeholder h)" if d.provisional else ""
    print(d.site_code, d.punch_date, d.employees, d.total_hours, flag)
```

`daily_hours` returns the clocked hours per site per day, split by PiType. It reads no names or
rates, and it is the denominator for cars per labor hour. An employee with both R and O
intervals on a day counts **once** in `employees`.

**Office accounts are excluded by default.** Employees whose home site is the office (99) clock
in at car-wash sites, rarely clock out, and produce most of the open punches. At one site in one
sample week they added 27 to 40 placeholder hours a day to about 30 real crew hours, which cut
CPLH by more than half. Their hours are reported in `excluded_hours` instead. Pass
`exclude_home_sites=None` to count everyone, or another tuple of home-site IDs.

**Open shifts make a day provisional.** `open_punches` counts the intervals that day that belong
to a shift with no real clock-out (`OPEN_CHAIN`, including a `-24:00` piece whose next-day
continuation is open), `open_hours` their placeholder hours, and `provisional` is
`open_punches > 0`. The statement reads one extra day past `end` to see those continuations.

## Productivity (cars per labor hour)

```python
rows = client.labor.productivity("2026-10-05", "2026-10-11", sites=24)
for r in rows:
    flag = " (provisional)" if r.provisional else ""
    print(r.log_date, r.car_count, r.labor_hours, r.cars_per_labor_hour,
          r.wash_revenue, r.revenue_per_labor_hour, flag)
```

`productivity` merges `client.sales.daily_summary`, `daily_hours` and one small statement for ARM
auto-recharge revenue on `(site, date)` in Python, running the statements one after the other.
The range limit is 31 days. A site-day with sales but no punches, or punches but no sales, is
still returned with zeros on the missing side. `cars_per_labor_hour` and
`revenue_per_labor_hour` are `None` when the day has no clocked hours. `open_punches`,
`open_hours` and `provisional` are carried through from `daily_hours`, and office accounts are
excluded the same way.

!!! warning "Revenue per labor hour leaves out ARM auto-recharges"
    Monthly ARM recharges are booked in a batch by the `Automatic Recharge` terminal around 06:20
    on billing days: over $11,000 at one site on one day, against about $1,000 of other revenue.
    They are not driven by labor, so `revenue_per_labor_hour` divides `wash_revenue`
    (`revenue - recharge_revenue`) by the hours. `revenue` still includes the recharges.

## Performance

Each method runs one statement. Measured on the live database for the six ICON sites:

| Call | Time |
|---|---|
| `timecard_totals` for two pay weeks | ~0.4 s |
| `labor_statistics` for one site and one week | ~0.2-0.3 s |
| `worked_employees` for two weeks | ~0.2 s |
| `daily_hours` for two weeks (interval rows, aggregated in Python) | ~0.3-0.6 s |
| `employees` for all ICON home sites | ~0.2 s |
