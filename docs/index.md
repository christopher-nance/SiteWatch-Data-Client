# SiteWatch Data Client

Read-only, Pydantic-typed Python client for the DRB SiteWatch POS database that runs WashU's
ICON Car Wash sites (Firebird 2.5, SQL dialect 1).

It wraps the SQL WashU runs most often (sites, daily sales and car counts, item mix, tenders,
ARM memberships, payroll timecards and labor statistics) in typed methods, and encodes
SiteWatch's hidden business rules once so callers can't get them wrong. It follows the same
patterns as the [Rinsed Snowflake Data Client](https://christopher-nance.github.io/Rinsed-Snowflake-Data-Client/)
and the [Sonny's Data API Client](https://christopher-nance.github.io/Sonnys-Data-API-Client/).

!!! warning "Live production database"
    The SiteWatch central database is shared by about 60 sites. Every statement runs in a
    read-only transaction, history methods require a bounded date range, and `query()` only
    accepts a single `SELECT`/`WITH` statement. Keep ad-hoc queries bounded by `Site` and a
    date range, and use `FIRST n`.

New here? Start with **[Getting Started](getting-started.md)**, then copy from **[Recipes](recipes.md)**. If something fails, see **[Troubleshooting](troubleshooting.md)**.

## Quick Start

```python
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()  # SITEWATCH_HOST, SITEWATCH_DATABASE, SITEWATCH_USER, SITEWATCH_PASSWORD, ...

with SiteWatchClient() as client:
    for day in client.sales.daily_summary("2026-10-05", "2026-10-11", sites=24):
        print(day.log_date, day.car_count, day.revenue)

    rows = client.labor.productivity("2026-10-05", "2026-10-11", sites=24)
    df = client.query("SELECT ID, Code, SiteName FROM V_Site WHERE ID = ?", [24])
```

## Resources

| Resource | What it covers | Guide |
|---|---|---|
| `client.sites` | Site IDs, codes, addresses, data freshness | [Sites](guides/sites.md) |
| `client.sales` | Daily/hourly sales, car count, item mix, tenders, sale detail | [Sales](guides/sales.md) |
| `client.catalog` | Items, report categories, terminals | [Catalog](guides/catalog.md) |
| `client.memberships` | ARM plans, activity, new members, member status, events | [Memberships](guides/memberships.md) |
| `client.labor` | Employees, timecards, labor statistics, daily hours, productivity | [Labor](guides/labor.md) |
| `client.query()` | Guarded, read-only raw SQL returning a DataFrame | [Client and raw queries](guides/client.md) |

## Installation

```bash
pip install git+https://github.com/christopher-nance/SiteWatch-Data-Client.git
```

Or from source for development:

```bash
git clone https://github.com/christopher-nance/SiteWatch-Data-Client.git
cd SiteWatch-Data-Client
pip install -e ".[dev,docs]"
```

You also need the 64-bit Firebird **2.5** client library (`fbclient.dll`). Point
`SITEWATCH_FB_CLIENT` at it.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `SITEWATCH_HOST` | yes | Query server host name or IP |
| `SITEWATCH_PORT` | no | Firebird port (default 3050) |
| `SITEWATCH_DATABASE` | yes | Server-side path of the `.FDB` file |
| `SITEWATCH_USER` | yes | Read-only user (normally `REPORTS`) |
| `SITEWATCH_PASSWORD` | yes | Password |
| `SITEWATCH_FB_CLIENT` | no | Absolute path to `fbclient.dll` |

Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

## Sites

By default every typed method covers the ICON car washes: Site IDs **5, 13, 18, 20, 24, 25**
(`ICON_SITE_IDS`). Office (99) and query server (100) are in `ICON_ALL_SITE_IDS`. The site
*code* (for example `ICONTN-030`) is not the site ID.

## Data freshness

SiteWatch replicates site data in packets, so the newest days can be missing. Check
`client.sites.data_freshness()` before reporting on a window that ends near today; a window that
runs past `latest_sale_date` silently returns partial data.
