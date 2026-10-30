# Getting Started

This page takes you from nothing to your first query in about ten minutes. After that, see
[Recipes](recipes.md) for ready-to-run exports and the per-resource guides for every option.

## 1. What you need

| Requirement | Notes |
|---|---|
| Windows PC with Python 3.10+ | 64-bit Python. Tested on 3.13. |
| Network access to the Query Server | TCP `3050` to the ICON Query Server's public IP. |
| SiteWatch `REPORTS` credentials | Read-only user created by DRB. Ask the repo owner. |
| Firebird **2.5** client library (`fbclient.dll`) | The server is Firebird 2.5. Newer clients need extra legacy-auth settings, so use 2.5. |
| Access to this private repo | Your GitHub account must be a collaborator. |

## 2. Install the Firebird 2.5 client

The client library isn't bundled with this package. Download the official **Firebird 2.5.9 x64
zip kit**, `Firebird-2.5.9.27139-0_x64.zip`, from the
[Firebird 2.5.9 release on GitHub](https://github.com/FirebirdSQL/firebird/releases/tag/R2_5_9)
and unzip it somewhere permanent, for example `C:\Firebird25`. The file you need is
`C:\Firebird25\bin\fbclient.dll`. Nothing has to be installed or registered.

## 3. Install the package

The repository is private, so pip needs your GitHub credentials. That means a logged-in `gh` CLI or
Git Credential Manager, which most Windows Git installs include.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install "git+https://github.com/christopher-nance/SiteWatch-Data-Client.git"
pip install python-dotenv   # optional, to load settings from a .env file
```

To pin a version, add `@v0.1.0` (or any tag or commit) to the URL. To work on the library itself,
clone it and run `pip install -e ".[dev,docs]"`.

## 4. Configure the connection

Create a `.env` file next to your script, and **never commit it**:

```ini
SITEWATCH_HOST=<query server IP or host name>
SITEWATCH_PORT=3050
SITEWATCH_DATABASE=<server-side path to SiteWatch.FDB>
SITEWATCH_USER=REPORTS
SITEWATCH_PASSWORD=<password>
SITEWATCH_FB_CLIENT=C:\Firebird25\bin\fbclient.dll
```

`.env.example` in the repo has the same keys. You can also pass everything to the constructor:
`SiteWatchClient(host=..., database=..., user=..., password=..., fb_client=...)`.

## 5. Your first query

```python
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()

with SiteWatchClient() as sw:
    for site in sw.sites.list():
        print(site.site_id, site.code, site.name)

    for f in sw.sites.data_freshness():
        print(f.site_code, "latest sale:", f.latest_sale_date, "latest punch:", f.latest_punch_date)
```

A list of the six ICON car-wash sites means everything works. If not, see
[Troubleshooting](troubleshooting.md).

## 6. Things to know before you query

- **Site filtering.** Every `sites=` argument accepts an `int`, a list of ints or `None`. `None`
  means the six ICON car-wash sites:

  | Site ID | Code | Name |
  |---|---|---|
  | 5 | ICONTN-005 | Madison |
  | 13 | ICONTN-015 | McMinnville |
  | 18 | ICONTN-018 | Decherd |
  | 20 | ICONTN-019 | Manchester |
  | 24 | ICONTN-030 | Nashville |
  | 25 | ICONTN-026 | Goodlettsville |

  The site ID isn't the store number (Site 13 is ICONTN-015), so use `site_code` in reports.
  The office (99) and query server (100) are only included when you ask for them.
- **Dates.** Pass `"YYYY-MM-DD"` strings, `date` or `datetime` objects. Ranges are inclusive and
  use the SiteWatch business date. Each history method caps the range, for example 31 days for
  sales and labor. Split longer periods into loops (see [Recipes](recipes.md)).
- **Data freshness.** The central database only has what the sites have replicated. Check
  `sw.sites.data_freshness()` before reporting on recent days. An empty result for recent dates
  usually means "not replicated yet", not "no business".
- **Read-only and shared.** This is the live POS database for about 60 sites. The client only
  runs read-only `SELECT`s and blocks write procedures and sensitive columns. Keep raw queries
  bounded by `Site` and a date range.
- **Results are Pydantic models.** Use attributes (`row.car_count`), `row.model_dump()` for
  dicts, or `pandas.DataFrame([r.model_dump() for r in rows])` for a table.

## Next steps

- [Recipes](recipes.md): payroll timecards to CSV, weekly sales to Excel, new members,
  productivity.
- Per-resource guides: [Sites](guides/sites.md), [Sales](guides/sales.md),
  [Catalog](guides/catalog.md), [Memberships](guides/memberships.md),
  [Labor and Timecards](guides/labor.md), [Client and Raw Queries](guides/client.md).
