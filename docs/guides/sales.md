# Sales

`client.sales` covers daily and hourly sales, SiteWatch's car count, the item and
category mix, tenders, sale-level detail and a status diagnostic. Every method needs an
inclusive date range on the **business (paid) date** and, apart from `list()`, defaults to
the ICON car-wash sites (5, 13, 18, 20, 24, 25).

## Quick start

```python
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()

with SiteWatchClient() as client:
    days = client.sales.daily_summary("2026-10-05", "2026-10-11", sites=24)
    for d in days:
        print(d.log_date, d.car_count, d.revenue)

    week = client.sales.totals("2026-10-05", "2026-10-11")
    print(week.car_count, week.revenue)
    for m in week.car_count_by_site:
        print(m.site_code, m.value)
```

## Methods

| Method | Returns | Range limit | Statements |
|---|---|---|---|
| `daily_summary(start, end, sites=None)` | `list[DailySales]` | 31 days | 3 (+ cached site-code lookup) |
| `totals(start, end, sites=None)` | `SalesTotals` | 31 days | runs `daily_summary` once |
| `hourly_summary(start, end, sites=None)` | `list[HourlySales]` | 14 days | 2 |
| `item_sales(start, end, sites=None, *, group_by, report_category, include_zero)` | `list[ItemSales]` | 31 days | 1 (+1 to resolve a category) |
| `tenders(start, end, sites=None, *, by_item=False)` | `list[TenderTotal]` | 31 days | 1 |
| `list(start, end, site, *, include_items=True, include_incomplete=False)` | `list[Sale]` | 3 days, one site | 2 |
| `status_mix(start, end, sites=None)` | `list[StatusCount]` | 14 days | 1 |

Dates can be `date`, `datetime` (the time is dropped) or ISO `YYYY-MM-DD` strings. Both
ends are inclusive. A longer range raises `ValidationError`; split it into smaller calls.

Typical timings at one site: about 1 s for a week of `daily_summary`, about 1.5 s for one
day of `list()` with lines.

## Definitions

These rules are applied for you. They were verified against live ICON data.

**Completed sale.** `f_IsCompleted(Status) = 1`, which means the status codes `C:P` (paid,
nothing to wash, for example ARM recharges and XPT cash events) and `C:PW` (paid and
washed). Excluded:

- `Notes`: ARM decline and expiry events. They carry a large nonzero `Total` that is not
  money taken.
- `I:P-W`: paid, wash still pending (see below).
- `PendAppr`: rare, $0.
- `E:-P`: failed recharge attempts. These have no `LogDate`, so no date filter ever finds
  them.

**Paid but wash pending (`I:P-W`).** These sales are paid and their tenders balance.
SiteWatch's car count includes them, but `sales_total` and `revenue` do not (the DRB
"completed only" rule). `daily_summary` reports them separately as `pending_wash_sales`
and `pending_wash_total`, so nothing is folded in silently.

**Car count.** SiteWatch's own statistic: the `TotalCarCount` sale fact in profit center 1.
It is one per sale that contains a Basic Washes item, covers retail and member ($0) washes,
includes `I:P-W` sales, and excludes recharges, plan sales without a wash and XPT events.
Retail vs member car count is **not** split in v1.

**Sales total and revenue.**

- `sales_total` is `SUM(V_Sale.Total)` of completed sales, SiteWatch's raw number.
- `deposits` are XPT cash-acceptor deposit lines (base type 170). They are cash movements,
  not revenue, and sit inside `Total`.
- `revenue = sales_total - deposits`.
- `paidouts` (base type 160) are ARM refunds. They stay **inside** revenue (revenue is net
  of refunds) and are also reported on their own, so gross is `revenue - paidouts`.
- `discounts` (base type 150) are ARM redemptions and LPM/employee discounts.
- `tendered` is `-SUM(Amt)` of tender lines (base type 180). On a balanced day it equals
  `sales_total`.

All money is rounded to 2 dp in Python. Firebird dialect 1 returns `NUMERIC(12,2)` as a
double, so a raw cast can come back as `2112.090000000001`.

## Daily summary

```python
rows = client.sales.daily_summary("2026-10-12", "2026-10-12")   # all ICON car washes
for r in rows:
    print(r.site_code, r.completed_sales, r.car_count, r.sales_total, r.revenue)
```

One row per site per business day that has a paid sale (or a car). `site_code` is the
`V_Site` code, such as `ICONTN-030`. It is **not** the site ID: site 13 is `ICONTN-015`.

Example `DailySales` (illustrative values):

```python
DailySales(site_id=24, site_code="ICONTN-030", log_date=date(2026, 10, 14),
           completed_sales=221, car_count=172, sales_total=2112.09, deposits=-76.0,
           revenue=2188.09, paidouts=-35.93, discounts=-2637.0, tendered=2112.09,
           pending_wash_sales=8, pending_wash_total=0.0)
```

## Totals

`totals()` sums `daily_summary` for the period. `car_count_by_site` and `revenue_by_site`
list every requested site, with 0 for a site that had no sales.

```python
t = client.sales.totals("2026-10-05", "2026-10-11", sites=[5, 24])
print(t.period_start, t.period_end, t.car_count, t.revenue, t.paidouts)
print({m.site_code: m.value for m in t.revenue_by_site})
```

## Hourly summary

```python
hours = client.sales.hourly_summary("2026-10-14", "2026-10-14", sites=24)
busiest = max(hours, key=lambda h: h.car_count)
print(busiest.hour, busiest.car_count)
```

The hour is the **paid** hour from `V_SaleStats.Hr`. Hours sum to the daily figures.

!!! note "Hour 6 is mostly ARM recharges"
    Automatic ARM recharges run at about 06:20 on the terminal "Automatic Recharge". They
    inflate `completed_sales` and `sales_total` for hour 6, but not `car_count`.

## Item and category mix

`item_sales()` is SWSLS06 without the GL column (`V_Item.GlAccount` is empty on every
item). Rows are always split by site and day.

```python
# wash package mix
mix = client.sales.item_sales(
    "2026-10-05", "2026-10-11", sites=24, report_category="Basic Washes"
)

# category totals for the whole Wash tree
cats = client.sales.item_sales(
    "2026-10-05", "2026-10-11", sites=24, group_by="category", report_category="Wash"
)

# discounts only
by_type = client.sales.item_sales("2026-10-05", "2026-10-11", sites=24, group_by="base_type")
discounts = [r for r in by_type if r.base_type == 150]
```

- `group_by` is `"item"` (default), `"category"` or `"base_type"`. Item columns are None
  unless grouping by item, and category columns are None when grouping by base type.
- `report_category` is a category **name**, case-insensitive. It is resolved to the
  category's branch and matched with `STARTING WITH`, so it includes every sub-category.
  An unknown name raises `ValidationError`.
- `include_zero=False` (the default) drops rows whose amount and quantity are both 0.
- `amount` is signed as stored: discounts and tenders are negative.
- Disabled items are included, because they still appear on historical sales.

Useful recipes:

| View | Call |
|---|---|
| Wash package mix | `report_category="Basic Washes"`, `group_by="item"` |
| Membership revenue | `report_category="ARM Plans Sold"` and `"ARM Plans Recharged"` |
| Discounts | `group_by="base_type"`, keep `base_type == 150` |
| Refunds | `group_by="base_type"`, keep `base_type == 160` |

!!! warning "Wash count vs header lines"
    "Basic Washes" (branch `001001001`) has a sub-category "Basic Wash Headers"
    (`001001001001`) with one $0 header line per package. To count washes from lines, use
    only the rows whose `category_branch == "001001001"`. That count equals
    `car_count - pending_wash_sales`.

!!! warning "Item names can be personal names"
    "Wash Notes" lines can carry an employee or customer name as the item name. Treat
    item-level results as internal.

## Tenders

```python
by_category = client.sales.tenders("2026-10-05", "2026-10-11", sites=24)
by_card = client.sales.tenders("2026-10-05", "2026-10-11", sites=24, by_item=True)
cards = [t for t in by_card if t.category == "Credit Card"]
```

Tender lines are base type 180 on completed sales, and `amount` is money received
(positive). They are named by report category and item, never through `V_Tender` (that
join is many-to-many: Visa and MC share one item). With `by_item=True` the grouping uses
the item ID, so two items with the same display name stay separate.

Caveats:

- `lines` counts tender lines, not customers or transactions.
- Cash at XPT kiosks appears as separate `CASH` and `CHANGE` lines (and a `Cash/Change`
  line at 0), so cash is reliable only at category level.
- The tender total for a day equals that day's `sales_total`.

## Sale detail

`list()` is SWSLS05 (long format) for one site and at most 3 days.

```python
sales = client.sales.list("2026-10-14", "2026-10-14", site=24)
for s in sales[:3]:
    print(s.receipt_number, s.status_code, s.paid_at, s.terminal_name, s.total)
    for line in s.items:
        print("   ", line.seq, line.item_name, line.amount)

recharges = [s for s in sales if s.terminal_name == "Automatic Recharge"]
```

- Completed sales only by default. `include_incomplete=True` adds `I:P-W`, `Notes` and
  `PendAppr` for diagnostics. `E:-P` never appears (no `LogDate`).
- `include_items=False` skips the second statement.
- `receipt_number` is `f_SaleScr(ObjID)`, the number on the receipt. `sale_id` is the
  internal ObjID and is not chronological.
- `paid_at` is `LogDate + LogTime / 14400.0`, in site-local wall-clock time.
- Customers are exposed as internal IDs only (`customer_code_id`, `customer_id`). The plate
  or tag text is never selected, and neither is the free-text line `Note`.
- `actual_sale_id` is raw. At ICON it is set on ARM recharge chains, not on edited sales,
  so do not use it as an "original sale" filter.
- Lines are ordered by `seq`. Package member lines carry `leader_seq`. All lines of a
  completed sale sum to 0, and the non-tender lines sum to `total`.

## Status mix

```python
mix = client.sales.status_mix("2026-10-14", "2026-10-14", sites=24)
print({m.status_code: (m.sales, m.total) for m in mix})
# e.g. {'C:P': (57, ...), 'C:PW': (164, ...), 'I:P-W': (8, ...), 'Notes': (217, ...)}
```

Use it to see what the completed-only filter removes from paid-date sales.

## Not in v1

- House-account charges (SWSLS07). They run through `V_SaleCharges`, which is slow. Use a
  bounded `client.query()` for one site and one day:

  ```python
  # about 6 s for one site-day; select explicit columns (card fields are blocked)
  df = client.query(
      "SELECT FIRST 500 c.SaleID, c.AcctType, c.Amount FROM V_SaleCharges c "
      "JOIN V_Sale s ON s.Site = c.Site AND s.ObjID = c.SaleID "
      "WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ? "
      "AND f_IsCompleted(s.Status) = 1 AND c.AcctType = ?",
      [24, date(2026, 10, 14), date(2026, 10, 15), "H"],
  )
  ```

- Retail vs member car count, service and wait times, and first-visit counts.

## Validation log

Checked live on 2026-10-30 (read-only, one statement at a time):

| Check | Window | Result |
|---|---|---|
| `daily_summary` tie-out | site 24, 2026-10-14 | 221 sales, 172 cars, 2112.09 total, -76 deposits, 2188.09 revenue, -35.93 paid-outs, 8 pending: all match |
| `daily_summary` vs independent `COUNT`/`SUM` by status code, base-type `SUM(Amt)` and Basic Washes sale count | site 24, 10-05..11 | every day matches |
| `daily_summary` default sites vs independent `COUNT`/`SUM` | 6 ICON sites, 10-12 | all 6 match |
| `totals` vs independent `COUNT`/`SUM` and the `CountedCarCount` PC 0 fact | sites 5 and 24, 10-05..11 | match |
| `hourly_summary` hours vs daily, hour 6 vs `LogTime` range | site 24, 10-05..11 | match |
| `item_sales` base-type totals (item, category and base-type groupings) vs independent `SUM(Amt)` | site 24, 10-05..11 | match |
| `tenders` vs `sales_total` and independent `-SUM(Amt)` | site 24, 10-05..11 | match |
| `list` count and total vs independent `COUNT`/`SUM`; lines balance | site 24, 10-14 | 221 sales, 0 unbalanced |
| `status_mix` vs raw `GROUP BY Status` | site 24, 10-01..14 | match |
