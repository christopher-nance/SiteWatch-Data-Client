# Recipes

Copy-paste examples for the jobs people ask for most. Each one assumes the setup from
[Getting Started](getting-started.md) (a `.env` with the `SITEWATCH_*` settings) and writes to an
`exports/` folder.

Shared setup used below:

```python
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()
OUT = Path("exports")
OUT.mkdir(exist_ok=True)


def to_frame(rows):
    """List of Pydantic models -> DataFrame."""
    return pd.DataFrame([r.model_dump() for r in rows])
```

## Payroll timecards for a pay period

Pay weeks run **Saturday to Friday**. `timecard_totals()` returns every interval, per-employee
totals and the warnings payroll should review: open punches, double-clocked overlaps and
partial weeks.

```python
with SiteWatchClient() as sw:
    report = sw.labor.timecard_totals("2026-09-26", "2026-10-09")   # two whole pay weeks

detail = to_frame(report.intervals)
detail["flags"] = detail["flags"].apply(lambda fl: ";".join(f.value for f in fl))
detail.to_csv(OUT / "timecards_detail.csv", index=False)

totals = to_frame(report.totals)
totals["weekly_hours_all_sites"] = totals["weekly_hours_all_sites"].apply(
    lambda weeks: "; ".join(f"{w['pay_week_start']}={w['hours']}" for w in weeks))
totals.to_csv(OUT / "timecards_totals.csv", index=False)

print(f"{len(report.intervals)} intervals, {detail['hours'].sum():.2f} h")
for w in report.warnings:
    print("WARNING:", w)
```

Flags on each interval: `OPEN` / `OPEN_CHAIN` (no real clock-out yet; fix it in SiteWatch before
paying), `EDITED` (changed in the Timecard Editor), `OVERLAP` (clocked in twice at once),
`SW_OVERTIME` (SiteWatch's own per-site overtime split) and `CROSSES_MIDNIGHT`.

SiteWatch applies the 40-hour overtime rule **per site**. If someone works at two ICON sites, use
`weekly_hours_all_sites` on each `TimecardTotal` (their hours per pay week across every
requested site) to check combined hours.

## Daily sales and car counts by site

```python
with SiteWatchClient() as sw:
    days = sw.sales.daily_summary("2026-10-05", "2026-10-11")   # all 6 ICON sites

df = to_frame(days)
pivot = df.pivot_table(index="log_date", columns="site_code", values="car_count", aggfunc="sum")
print(pivot)
df.to_csv(OUT / "daily_sales.csv", index=False)
# For Excel: pip install openpyxl, then df.to_excel(OUT / "daily_sales.xlsx", index=False)
```

`car_count` is SiteWatch's own car count and `revenue` is completed-sale revenue.
`pending_wash_sales` counts sales that were paid but not yet washed. Field definitions are in
the [Sales guide](guides/sales.md).

## Longer periods (methods are capped at 31 days)

```python
def daterange_chunks(start: date, end: date, days: int = 31):
    while start <= end:
        stop = min(start + timedelta(days=days - 1), end)
        yield start, stop
        start = stop + timedelta(days=1)


rows = []
with SiteWatchClient() as sw:
    for a, b in daterange_chunks(date(2026, 8, 1), date(2026, 10, 14)):
        rows += sw.sales.daily_summary(a, b)
to_frame(rows).to_csv(OUT / "daily_sales_aug_oct.csv", index=False)
```

## New members (with contact details)

```python
with SiteWatchClient() as sw:
    members = sw.memberships.new_members("2026-10-01", "2026-10-14", include_contact=True)

rows = []
for m in members:
    row = m.model_dump(exclude={"contact"})
    if m.contact:
        row.update({f"contact_{k}": v for k, v in m.contact.model_dump().items()})
    rows.append(row)
pd.DataFrame(rows).to_csv(OUT / "new_members.csv", index=False)
print(len(members), "joins,", sum(m.is_rejoin for m in members), "rejoins")
```

Pass `first_join_only=True` to leave out returning members. Contact details are only returned
with `include_contact=True`, so treat the CSV as customer PII.

## Membership activity for a period

```python
with SiteWatchClient() as sw:
    t = sw.memberships.activity_totals("2026-10-01", "2026-10-14")
print(f"joins {t.joins}, recharges {t.recharges} (${t.recharge_amount:,.2f}), "
      f"terminated {t.terminated}, declined {t.declined}")
```

## Labor productivity (cars and revenue per labor hour)

```python
with SiteWatchClient() as sw:
    prod = sw.labor.productivity("2026-10-05", "2026-10-11")

df = to_frame(prod)
print(df[["log_date", "site_code", "car_count", "labor_hours", "cars_per_labor_hour",
          "revenue_per_labor_hour", "provisional"]])
```

Office accounts (home site 99) are left out of labor hours by default
(`exclude_home_sites=(99,)`). Revenue per labor hour uses wash revenue, so automatic membership
recharges aren't counted. Rows marked `provisional` include shifts with no clock-out yet.

## Top-selling items

```python
with SiteWatchClient() as sw:
    items = sw.sales.item_sales("2026-10-05", "2026-10-11", sites=24)

df = to_frame(items)
top = df.groupby("item_name")[["quantity", "amount"]].sum().sort_values("amount", ascending=False)
print(top.head(15))
```

Use `group_by="category"` for report-category totals, or `sales.tenders()` for the payment mix
(cash, card, house account).

## Raw SQL when no method fits

```python
with SiteWatchClient() as sw:
    df = sw.query(
        "SELECT FIRST 20 s.ObjID, s.LogDate, s.Total FROM V_Sale s "
        "WHERE s.Site = ? AND s.LogDate = ? ORDER BY s.Total DESC",
        [24, "2026-10-14"],
    )
print(df)
```

`query()` accepts a single read-only `SELECT`, returns a DataFrame with lower-case column names,
and always takes values as `?` parameters. Keep it bounded by `Site` and a date. See
[Client and Raw Queries](guides/client.md) and the DRB ODBC guide for view and column names.
