# Sites

`client.sites` reads the SiteWatch site master (`V_Site` joined to `V_System`) and tells you
how current the central database is for each site.

| Method | Returns | Statements |
|---|---|---|
| `list(sites=None, *, include_office=False)` | `list[Site]` | 1 |
| `get(site_id)` | `Site` | 1 |
| `data_freshness(sites=None)` | `list[SiteFreshness]` | 1 + 2 per site + 1 |

## Basic Usage

```python
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()

with SiteWatchClient() as client:
    for site in client.sites.list():
        print(f"{site.site_id:>3}  {site.code:<12} {site.name}  ({site.city}, {site.state})")
```

```
  5  ICONTN-005   Example Car Wash- Madison  (Exampleville, TN)
 13  ICONTN-015   Example Car Wash- ...      (...)
...
```

## Site IDs vs site codes

Every method in this package takes **site IDs** (`V_Site.ID`), not site codes. The two
don't match: site 13 is `ICONTN-015`, and site 24 is `ICONTN-030`. Results that carry a
`site_id` also carry `site_code` so reports can show the code.

| Constant | Sites | Meaning |
|---|---|---|
| `ICON_SITE_IDS` | 5, 13, 18, 20, 24, 25 | ICON car washes: the default for every `sites=None` |
| `ICON_ALL_SITE_IDS` | the above plus 99, 100 | Adds the office (99) and the query server (100), which have no current sales or punches |

Legacy `OLD-*` sites share the same database. They are never returned by default, but you can
ask for them by ID.

```python
with SiteWatchClient() as client:
    icon = client.sites.list()                       # 6 car washes
    everything = client.sites.list(include_office=True)  # adds 99 and 100
    two = client.sites.list([5, 24])                  # exactly these
    one = client.sites.get(24)
    print(one.code)                                   # ICONTN-030
```

`list()` sorts and de-duplicates the IDs you pass. IDs that don't exist are simply missing from
the result. `get()` raises `ValidationError` for an unknown ID.

## Site Object Fields

| Field | Type | Source | Description |
|---|---|---|---|
| `site_id` | `int` | `V_Site.ID` | SiteWatch site ID |
| `code` | `str` | `V_Site.Code` | Site code, for example `ICONTN-030` |
| `name` | `str` | `V_Site.SiteName` | Site name, trimmed |
| `enabled` | `bool` | `V_Site.Enabled` | True when `Enabled = 1` (1 Enabled, 2 Disabled) |
| `address` | `str \| None` | `V_System.Address` | Street address |
| `address2` | `str \| None` | `V_System.Address2` | Second address line |
| `city` | `str \| None` | `V_System.City` | City exactly as typed at the site (not normalized) |
| `state` | `str \| None` | `V_System.State` | State abbreviation |
| `zip` | `str \| None` | `V_System.Zip` | ZIP code |
| `phone` | `str \| None` | `V_System.Phone` | Phone number |

`V_System.Name` is always `"System"`, so it is never used as the site name. `V_Site` is read with
an explicit column list only, because the view also holds email, SMTP and card-processor
credentials.

## Data freshness

The central database lags the sites. At the time of writing, sales ended on 2026-10-15 and
punches on 2026-10-16. If a recent date range comes back empty, check freshness before you
suspect the query.

```python
with SiteWatchClient() as client:
    for f in client.sites.data_freshness():
        print(f"{f.site_code:<12} sales through {f.latest_sale_date}  "
              f"punches through {f.latest_punch_date}")
```

```
ICONTN-005   sales through 2026-10-15  punches through 2026-10-16
ICONTN-015   sales through 2026-10-15  punches through 2026-10-16
...
```

### SiteFreshness fields

| Field | Type | Description |
|---|---|---|
| `site_id` | `int` | SiteWatch site ID |
| `site_code` | `str` | Site code |
| `latest_sale_date` | `date \| None` | Newest `V_Sale.LogDate` in the last 400 days |
| `latest_punch_date` | `date \| None` | Newest `V_PunchInterval.PunchDate` in the last 400 days |
| `replication_applied_at` | `datetime \| None` | `V_Replication.PacketAppliedDate`, in site-local wall-clock time |

### How it works

For each site, one at a time, the method runs two `MAX()` probes, each with a date floor of
today minus 400 days:

```sql
SELECT MAX(s.LogDate) FROM V_Sale s WHERE s.Site = ? AND s.LogDate >= ?
SELECT MAX(p.PunchDate) FROM V_PunchInterval p WHERE p.Site = ? AND p.PunchDate >= ?
```

The floor is computed in Python and sent as a parameter, so the SQL never depends on the
server's `TODAY`. Each sale probe takes about 0.8 s and each punch probe about 0.2 s. The
`SELECT FIRST 1 ... ORDER BY LogDate DESC` form on `V_SaleStats` took about 20 s per site and is
deliberately not used. A full six-site call takes about 7 s, or up to about 20 s on a cold
server cache. A site with nothing inside the floor reports `None`.

### Replication time

At ICON, only the office hub (site 99) carries packet dates in `V_Replication`. The car-wash
sites' rows exist but hold NULL, so their `replication_applied_at` is `None`. Ask for site 99 to
see when the last packet reached the central database:

```python
with SiteWatchClient() as client:
    (hub,) = client.sites.data_freshness(99)
    print(f"last packet applied {hub.replication_applied_at:%Y-%m-%d %H:%M}")
```

## Use Cases

### Clamp a report window to the available data

```python
from datetime import date

with SiteWatchClient() as client:
    fresh = client.sites.data_freshness()
    available_through = min(f.latest_sale_date for f in fresh if f.latest_sale_date)
    end = min(date(2026, 10, 31), available_through)
    print(f"Reporting through {end}")
```

### Map site IDs to codes for a report

```python
with SiteWatchClient() as client:
    code_by_id = {s.site_id: s.code for s in client.sites.list()}
```

### Export sites to JSON

```python
import json
from pathlib import Path

with SiteWatchClient() as client:
    data = [s.model_dump(mode="json") for s in client.sites.list(include_office=True)]

Path("exports").mkdir(exist_ok=True)  # exports/ is gitignored
with open("exports/sites.json", "w") as f:
    json.dump(data, f, indent=2)
```

## Errors

| Situation | Exception |
|---|---|
| A site ID that isn't a positive `int` (bools and strings are rejected), or an empty list | `ValidationError` |
| `get()` with an unknown ID | `ValidationError` |
| `data_freshness()` with an unknown ID | `ValidationError` (raised before any probe runs) |
| Not connected | `ConnectionError` |
| The database rejects or fails a statement | `QueryError` |

## Data Source

- `V_Site` (59 rows, key `ID`): identity, enabled flag.
- `V_System` (59 rows, key `Site`): address and phone. It joins 1:1 on `V_System.Site = V_Site.ID`.
- `V_Sale`, `V_PunchInterval`: freshness probes, bounded by site and a 400-day floor.
- `V_Replication` (1 row per site): replication packet status.
