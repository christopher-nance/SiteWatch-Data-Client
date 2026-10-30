# SiteWatch Data Client

Read-only, Pydantic-typed Python client for WashU Carwash's DRB SiteWatch POS database (ICON Car Wash sites,
Firebird 2.5, SQL dialect 1).

It wraps the SiteWatch SQL WashU runs most often (sites, daily sales and car counts, item mix, tenders, ARM
memberships, payroll timecards and labor statistics) in typed methods that return Pydantic models, and encodes
SiteWatch's hidden business rules once (completed-sale status, the `TotalCarCount` fact, tender signs, dialect-1
dates, punch classification) so callers can't get them wrong. It follows the same patterns as the
[Rinsed Snowflake Data Client](https://github.com/christopher-nance/Rinsed-Snowflake-Data-Client) and the
[Sonny's Data API Client](https://github.com/christopher-nance/Sonnys-Data-API-Client).

> **Live production database.** The SiteWatch central database is shared by about 60 sites. Every statement runs in
> a read-only transaction, every history method requires a bounded date range, and `query()` only accepts a single
> `SELECT`/`WITH` statement.

## Installation

```bash
pip install git+https://github.com/christopher-nance/SiteWatch-Data-Client.git
```

Or from a built wheel (`python -m build` or `hatch build` produces it in `dist/`):

```bash
pip install sitewatch_data_client-0.1.0-py3-none-any.whl
```

Or from source for development:

```bash
git clone https://github.com/christopher-nance/SiteWatch-Data-Client.git
cd SiteWatch-Data-Client
pip install -e ".[dev,docs]"
```

Requires Python 3.10+. Dependencies: `fdb`, `pydantic` 2 and `pandas` 2.

### Firebird 2.5 client library

`fdb` needs the native Firebird client. Install the 64-bit **Firebird 2.5** client (`fbclient.dll` on Windows) and
point `SITEWATCH_FB_CLIENT` at it. Without it, fdb falls back to its default library lookup, which may find a newer
client that cannot authenticate against the 2.5 server.

## Quick Start

```python
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()  # the library reads SITEWATCH_* env vars; it never reads .env itself

with SiteWatchClient() as client:
    # Sites
    for site in client.sites.list():
        print(site.site_id, site.code, site.name)

    # Daily sales and car counts (defaults to the six ICON car washes)
    for day in client.sales.daily_summary("2026-10-05", "2026-10-11"):
        print(day.site_code, day.log_date, day.car_count, day.revenue)

    week = client.sales.totals("2026-10-05", "2026-10-11")
    print(f"Cars: {week.car_count}  Revenue: ${week.revenue:,.2f}")
    for m in week.car_count_by_site:
        print(f"  {m.site_code}: {m.value:.0f}")

    # Memberships (ARM)
    t = client.memberships.activity_totals("2026-10-01", "2026-10-14")
    print(t.joins, t.recharges, t.declined)

    # Payroll timecards (Saturday-Friday pay weeks)
    report = client.labor.timecard_totals("2026-09-26", "2026-10-09")
    for row in report.totals:
        print(row.site_code, row.employee_id, row.total_hours, row.sw_overtime_hours)

    # Raw SQL escape hatch (returns a pandas DataFrame)
    df = client.query("SELECT ID, Code, SiteName FROM V_Site WHERE ID = ?", [24])
```

## Configuration

Set these environment variables (copy `.env.example` to `.env` and load it with `python-dotenv`), or pass the
values to `SiteWatchClient(...)` directly:

| Variable | Required | Description |
|---|---|---|
| `SITEWATCH_HOST` | Yes | Query server host name or IP |
| `SITEWATCH_PORT` | No | Firebird port (default `3050`) |
| `SITEWATCH_DATABASE` | Yes | Server-side path of the `.FDB` file |
| `SITEWATCH_USER` | Yes | Read-only Firebird user (normally `REPORTS`) |
| `SITEWATCH_PASSWORD` | Yes | Password |
| `SITEWATCH_FB_CLIENT` | No | Absolute path to the Firebird 2.5 `fbclient.dll` |

```python
client = SiteWatchClient(
    host="your-query-server",
    database=r"E:\path\to\SiteWatch.FDB",
    user="REPORTS",
    password="...",
    fb_client=r"C:\path\to\fbclient.dll",
)
client.connect()
try:
    ...
finally:
    client.close()
```

Never commit `.env`. The password never appears in `repr()`, logs or exception messages.

## Available Methods

Dates accept `date`, `datetime` or ISO `YYYY-MM-DD` strings, and both ends are inclusive. A range longer than the
method's limit raises `ValidationError`; split it into smaller calls.

### `client.sites`

| Method | Returns | Description |
|---|---|---|
| `list(sites=None, *, include_office=False)` | `list[Site]` | Site ID, code, name and address |
| `get(site_id)` | `Site` | One site |
| `data_freshness(sites=None)` | `list[SiteFreshness]` | Latest sale and punch date per site, replication time |

### `client.sales`

| Method | Returns | Limit | Description |
|---|---|---|---|
| `daily_summary(start, end, sites=None)` | `list[DailySales]` | 31 d | Sales, car count, revenue, deposits and tenders per site-day |
| `totals(start, end, sites=None)` | `SalesTotals` | 31 d | Period totals with per-site breakdowns |
| `hourly_summary(start, end, sites=None)` | `list[HourlySales]` | 14 d | Sales and car count by hour |
| `item_sales(start, end, sites=None, *, group_by, report_category, include_zero)` | `list[ItemSales]` | 31 d | Item, category or base-type mix |
| `tenders(start, end, sites=None, *, by_item=False)` | `list[TenderTotal]` | 31 d | Tender mix (card, cash, house account ...) |
| `list(start, end, site, *, include_items=True, include_incomplete=False)` | `list[Sale]` | 3 d, 1 site | Sale headers with lines |
| `status_mix(start, end, sites=None)` | `list[StatusCount]` | 14 d | Sale status diagnostic |

### `client.catalog`

| Method | Returns | Description |
|---|---|---|
| `items(*, include_disabled=False, report_category=None)` | `list[Item]` | Items with type, base type and report category |
| `report_categories()` | `list[ReportCategory]` | Report category tree |
| `terminals(sites=None, *, include_disabled=False)` | `list[Terminal]` | Terminals (lanes, kiosks, registers) |

### `client.memberships`

| Method | Returns | Limit | Description |
|---|---|---|---|
| `plans(*, include_disabled=False)` | `list[MembershipPlan]` | n/a | ARM plans |
| `activity(start, end, sites=None, *, by_plan=True)` | `list[MembershipActivity]` | 93 d | Joins, recharges, uses, declines and expiries per day |
| `activity_totals(start, end, sites=None)` | `MembershipActivityTotals` | 93 d | Period totals |
| `new_members(start, end, sites=None, *, include_contact=False, first_join_only=False, ...)` | `list[NewMember]` | 31 d | Paid ARM joins incl. rejoins (contact details opt-in) |
| `member_status(customer_id, as_of=None)` | `MemberStatus \| None` | n/a | One member's plan and status |
| `events(start, end, site, *, statuses=None)` | `list[MembershipEvent]` | 7 d, 1 site | Row-level ARM events |

### `client.labor`

| Method | Returns | Limit | Description |
|---|---|---|---|
| `employees(sites=None, *, active_only=True, employee_ids=None)` | `list[Employee]` | n/a | Employees by home site (no secrets) |
| `worked_employees(start, end, sites=None)` | `list[Employee]` | 93 d | Everyone who punched at the sites |
| `timecards(start, end, sites=None, *, pi_types=("R", "O"))` | `list[TimecardInterval]` | 31 d | Classified punch intervals with flags |
| `timecard_totals(start, end, sites=None)` | `TimecardReport` | 31 d | Per-employee totals, pay-week hours and warnings |
| `labor_statistics(start, end, site, *, group_by=("employee",), ...)` | `list[LaborStat]` | 31 d | SiteWatch's own labor statistics |
| `daily_hours(start, end, sites=None, *, exclude_home_sites=(99,))` | `list[DailyLaborHours]` | 93 d | Clocked hours per site-day (office accounts excluded, open shifts flagged) |
| `productivity(start, end, sites=None, *, exclude_home_sites=(99,))` | `list[DailyProductivity]` | 31 d | Cars and wash revenue per labor hour (ARM auto-recharges left out) |

## Site Filtering

Every `sites=` argument defaults to the ICON car washes: Site IDs **5, 13, 18, 20, 24, 25** (`ICON_SITE_IDS`).
The office (99) and the query server (100) are in `ICON_ALL_SITE_IDS`. The site *code* (for example `ICONTN-030`)
is not the site ID, and every row-level model carries both `site_id` and `site_code`.

```python
# All ICON car washes (default)
client.sales.daily_summary("2026-10-05", "2026-10-11")

# One site
client.sales.daily_summary("2026-10-05", "2026-10-11", sites=24)

# Several sites
client.sales.daily_summary("2026-10-05", "2026-10-11", sites=[5, 24])
```

Legacy `OLD-*` sites share the database and can be passed explicitly by ID.

## Raw Queries

`client.query(sql, params=None, *, max_rows=10000)` runs ad-hoc SQL and returns a pandas DataFrame with lower-case
column names and trimmed strings. Pass every value as a `?` parameter:

```python
from datetime import date

df = client.query(
    "SELECT FIRST 10 ObjID, LogDate, Total FROM V_Sale "
    "WHERE Site = ? AND LogDate >= ? AND LogDate < ?",
    [24, date(2026, 10, 14), date(2026, 10, 15)],
)
```

Firebird dialect 1 tips: use `FIRST n` (not `LIMIT` or `TOP`); `DATE` columns are timestamps, so filter half-open;
use `f_IsCompleted(Status) = 1` for completed sales; and use a derived table plus `JOIN` instead of
`IN (SELECT ...)`. A warning is logged when `max_rows` truncates a result; `max_rows=None` removes the cap.

`max_rows` limits only the rows fetched, **not the work the server does**: `SELECT COUNT(*) FROM V_Sale` still scans
all history, and there is no statement timeout or cancel. `query()` logs a warning when a history view (`V_Sale`,
`V_SaleItems`, `V_PunchInterval`, ...) is read without a `WHERE` clause, and when the SQL calls a procedure known to run
for minutes (`Arm_Monthly_Current_Status`, about 8 minutes for one site; `Arm_Summary_For_Date_Range`;
`CustClubPlan_Balances`).

## Read-Only and Safety

- Every statement runs in a read-only, read-committed transaction that is always rolled back (one reusable
  transaction per connection, so a long-lived client does not accumulate transaction objects).
- `query()` is checked by a lexical guard before anything reaches the server: one `SELECT`/`WITH` statement only, no
  DML/DDL and no `EXECUTE PROCEDURE`. Stored procedures and UDFs are **allowlisted**: only the 11 procedures and 30
  `f_*` UDFs that DRB documents may be called (UDFs run as native server code outside transaction control), and
  `GEN_ID` / `NEXT VALUE FOR` are rejected because generator increments survive a rollback. No employee secrets
  (`SSNUM`, `PWORD`, `SECURITYCODE`, `DRVLNUM`), no `V_Site` credential columns, no card data or payment tokens
  (`MASKEDPAN`, `TOKEN`, `CVVR`), and no `SELECT *` (in any spelling, such as `FIRST(1)*` or `DISTINCT*`) on views
  that hold them. A violation raises `UnsafeQueryError`.
- Typed methods are fully parameterized; no caller input is ever formatted into SQL.
- Customer contact details are returned only on request (`memberships.new_members(include_contact=True)`).
- Keep ad-hoc queries bounded by `Site` and a date range, and run them one at a time.

## Data Freshness

SiteWatch replicates site data to the central database in packets, so the newest data can lag behind the calendar.
Check before reporting:

```python
for f in client.sites.data_freshness():
    print(f.site_code, f.latest_sale_date, f.latest_punch_date)
```

A window that ends after `latest_sale_date` silently returns partial data for the missing days.
`timecard_totals()` also reports the `latest_punch_date` it saw. At the car-wash sites
`replication_applied_at` is usually `None`; the office hub (site 99) carries the packet time.

## Error Handling

```python
from sitewatch_data_client import SiteWatchClient, SiteWatchError, ValidationError

try:
    with SiteWatchClient() as client:
        client.sales.daily_summary("2026-09-01", "2026-10-14")  # more than 31 days
except ValidationError as e:
    print(f"Bad input: {e}")
except SiteWatchError as e:
    print(f"Client error: {e}")
```

Hierarchy: `SiteWatchError` > `ConfigurationError`, `ConnectionError`, `QueryError`, `ValidationError` >
`UnsafeQueryError`.

## Documentation

Start here (these render directly on GitHub):

| Guide | What's in it |
|---|---|
| [Getting Started](docs/getting-started.md) | Install, Firebird 2.5 client, `.env` setup, first query, site/date rules |
| [Recipes](docs/recipes.md) | Payroll timecards to CSV, daily sales and car counts, new members, membership activity, productivity, item mix, raw SQL |
| [Troubleshooting](docs/troubleshooting.md) | Connection, permission, safety-guard and data-freshness errors |
| Resource guides | [Sites](docs/guides/sites.md), [Sales](docs/guides/sales.md), [Catalog](docs/guides/catalog.md), [Memberships](docs/guides/memberships.md), [Labor and Timecards](docs/guides/labor.md), [Client and Raw Queries](docs/guides/client.md) |

To browse the full site with the generated API reference, run `pip install -e ".[docs]"` and then
`python -m mkdocs serve`, and open http://127.0.0.1:8000. The repo is private, so the docs aren't
published to GitHub Pages.

## Development

```bash
pip install -e ".[dev,docs]"
python -m ruff check src tests
python -m pytest                              # unit tests, no database
SITEWATCH_LIVE=1 python -m pytest tests/live  # bounded live checks, needs .env
python -m mkdocs build --strict
```

## License

Wash Associates Business Internal Use License 1.0. See `LICENSE`.
