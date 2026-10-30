# CLAUDE.md

## Project Overview

SiteWatch Data Client: a pip-installable, **read-only** Python package for WashU Carwash's DRB SiteWatch POS database
(ICON Car Wash, Firebird 2.5, SQL dialect 1, charset `NONE`, read-only user `REPORTS`). It wraps commonly used
SiteWatch SQL in typed methods returning Pydantic models. It mirrors the Rinsed Snowflake Data Client
(`client.<resource>.<method>()`, `cached_property` resources, `query()` escape hatch) and the Sonny's clients
(Google-style docstrings, one docs guide per resource).

- **Package name:** `sitewatch-data-client`
- **Import:** `sitewatch_data_client`
- **Main class:** `SiteWatchClient`
- **Install:** `pip install git+https://github.com/christopher-nance/SiteWatch-Data-Client.git`
- **Design spec:** `docs/superpowers/specs/2026-10-30-sitewatch-data-client-design.md` (source of truth for methods,
  SQL and business rules)

## Before Committing to GitHub

1. **Update the changelog:** add an entry to `CHANGELOG.md` under the new version describing what changed.
2. **Bump the version number** in **both** files:
   - `pyproject.toml` → `version = "x.y.z"`
   - `src/sitewatch_data_client/_version.py` → `__version__ = "x.y.z"`
3. **Redeploy docs** if any docs files changed: `python -m mkdocs gh-deploy --force`
4. Confirm `.env`, exports and any real customer/employee data are **not** staged.

Users install via `pip install git+...`, which caches by version: without a bump, they won't get new code.

## Versioning

Follow semver:
- **Patch** (0.1.1): bug fixes, no API changes
- **Minor** (0.2.0): new features, backward-compatible
- **Major** (1.0.0): breaking API changes

## Key Architecture

- `src/sitewatch_data_client/`: package source (src layout, hatchling)
- `_client.py`: `SiteWatchClient`, resources as `functools.cached_property`, `connect`/`close`/context manager,
  `_execute` (trusted library SQL) and `query()` (guarded, `max_rows` cap, warns on truncation)
- `_config.py`: `SiteWatchConfig` dataclass, `load_config()` from `SITEWATCH_*` env vars (the library never reads
  `.env` itself; callers use python-dotenv)
- `_connection.py`: `fdb.connect(sql_dialect=1, charset="NONE", fb_library_name=...)`; **one read-only transaction
  per statement** (`ISOLATION_LEVEL_READ_COMMITED_RO`, always rolled back); lower-case columns; CHAR padding
  stripped; errors wrapped in `QueryError` with a SQL preview, never the params or password
- `_safety.py`: `assert_read_only_sql()` and the blocked procedure / sensitive column lists
- `_filters.py`: `normalize_date`, `normalize_date_range` (inclusive in, half-open out, day caps),
  `normalize_sites` (default ICON car washes), `placeholders()`, `build_in_clause()`
- `_constants.py`: ICON site IDs, base types, ARM plan type and statuses, punch codes, `RANGE_LIMITS`
- `resources/_x.py` + `types/_x.py`: one pair per area (`sites`, `sales`, `catalog`, `memberships`, `labor`). Each
  resource module holds its own SQL as module-level `*_sql(...)` builders returning `(sql, params)` (or SQL
  constants in `_catalog.py`), so they are unit-testable without a database. (The design spec's single
  `_query_builder.py` was not adopted.) Resource modules start with `from __future__ import annotations` (a method
  named `list` otherwise breaks `list[...]` annotations)
- `types/_common.py`: models shared across resources (`SiteMetric`)
- Exports: resources in `resources/__init__.py`, every model in `types/__init__.py`, and client, config,
  constants, exceptions, resources, models and `__version__` in the package `__init__.py` (explicit `__all__`;
  `tests/test_integration.py` checks they resolve)
- `labor.productivity()` merges `sales.daily_summary()` and `labor.daily_hours()` in Python (no SQL of its own)
- Docs: `docs/guides/{client,sites,sales,catalog,memberships,labor}.md`, `docs/api/*.md` (mkdocstrings). Add new
  guides to the `mkdocs.yml` nav and new models to `docs/api/models.md`; `mkdocs build --strict` must pass.
  `docs/superpowers/` (design spec) is excluded from the built site.

## Safety Rules (non-negotiable)

- Live production database shared by about 60 sites. Read-only transactions only; never `EXECUTE PROCEDURE`.
- **Never call** (some are selectable, so even a `SELECT` runs them): `DELETE_INVENTORY_DATA`, `DELETE_SALE_DATA`,
  `DELETE_SECURITY_DATA`, `DELETE_SPECIFIC_SALE`, `DELETE_TRAINING_SALES`, `GEN_OBJID`, `PREPAID_ACTIVATE_CODES`,
  `PREPAID_CREATE_TEMP_CODES`, `PREPAID_DEACTIVATE_CODES`, `PREPAID_SET_BALANCE`, `PREPAID_UPDATE_BALANCE`,
  `PREPAID_UPDATE_DAY_BALANCES`, `CUSTPPD_UPDATE_DAY_BALANCES`, `CUSTPROMO_UPDATE_BALANCE`,
  `CUSTPROMO_UPDATE_DAY_BALANCES`.
- Parameterize every user value with `?` (fdb qmark). Never format user input into SQL. Site lists go through
  `normalize_sites` + `placeholders()`.
- Every history method requires `start`/`end` (validated and capped by `RANGE_LIMITS`) and defaults `sites` to
  `ICON_SITE_IDS = (5, 13, 18, 20, 24, 25)`.
- Load etiquette: one query at a time, no `IN (SELECT ...)` (use derived table + `JOIN`), no unbounded `COUNT(*)`
  on history views, `FIRST n` not `LIMIT`.
- Privacy: never select `SSNUM`, `PWORD`, `SECURITYCODE`, `DRVLNUM` (or web password hash/salt); `V_Employee` and
  `V_Site` by explicit column list only. Customer PII only from explicit customer/member methods. No card data.
- Never print the password; never put credentials in code, docs, tests or committed files.

## Business Logic Notes

Read these before writing SQL. They were verified against live data:

- `C:/Users/chris/Downloads/DRB & Claude/CLAUDE.md`: connection, safety rules, ICON site IDs, verified joins,
  gotchas, Firebird versions of the Appendix C queries, punch semantics, the 117 readable views.
- `C:/Users/chris/source/ODBC_Remote_Tester/docs/SiteWatch_ODBC_Schema_Map.md` and `docs/schema/*.md`,
  `catalog.json`: verified joins with evidence, column semantics.
- `C:/Users/chris/source/ODBC_Remote_Tester/export_timecards.py`: verified payroll timecard logic (punch
  classification incl. the `same_minute` rounding rule, OPEN/EDITED/OVERLAP/SW_OVERTIME/CROSSES_MIDNIGHT flags,
  Sat-Fri pay weeks). `export_members.ps1`: member join export SQL.
- Key facts:
  - ICON car-wash Site IDs 5, 13, 18, 20, 24, 25 (99 office, 100 query server). Site code (`ICONTN-*`) is not the
    site ID. Legacy `OLD-*` sites share the database.
  - Dialect 1: `DATE` is a timestamp and `LogDate`/`PunchDate` are midnight, so filter half-open
    (`LogDate >= ? AND LogDate < ?`). Paid timestamp = `LogDate + LogTime/14400.0`.
  - Completed sales: `f_IsCompleted(Status) = 1`. Car count is the `TotalCarCount` fact.
  - Tender lines are base type 180 with the opposite sign to the payment.
  - Money: `CAST(... AS NUMERIC(12,2))` **and** round in Python (dialect-1 float noise survives the cast).
  - Punch `InTime`/`OutTime` are minutes after midnight; pay weeks run Saturday to Friday.
  - Data currently ends around 2026-10-15 (sales) / 2026-10-16 (punches). Use 2026-10-01..14 windows for live
    checks.

## Testing

- `python -m pytest`: unit tests with a mocked connection (`tests/conftest.py` `mock_client` patches
  `SiteWatchConfig.load_config` and sets `client._conn = MagicMock()`; `make_df` builds return frames). Assert the
  SQL has placeholders and the params tuple carries the values.
- `SITEWATCH_LIVE=1 python -m pytest tests/live -v`: live tests, loading `.env` via python-dotenv. Keep them
  bounded: one site for up to 7 days, or the six ICON sites for at most one 14-day pay period (the timecard
  tie-out uses 2026-09-26..10-09), plus `FIRST n` on raw queries. Run them one at a time and assert aggregates
  only. Never call `Arm_Monthly_Current_Status`, `Arm_Summary_For_Date_Range` or `CustClubPlan_Balances` from a
  test (minutes per call).
- Fixtures, tests and docs use made-up values only ("Pat Example", employee 99999). Never real customer or
  employee data.
- `python -m ruff check .` and `python -m ruff format .` before committing (Markdown is excluded in
  `pyproject.toml`, so doc examples keep their hand formatting).
