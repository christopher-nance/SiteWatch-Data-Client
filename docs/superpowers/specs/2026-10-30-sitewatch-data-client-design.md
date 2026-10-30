# sitewatch-data-client: design spec

- **Date:** 2026-10-30
- **Status:** Draft for implementation
- **Package:** distribution `sitewatch-data-client`, import `sitewatch_data_client`, main class `SiteWatchClient`
- **Source system:** DRB SiteWatch central database for ICON Car Wash, Firebird 2.5, SQL dialect 1, charset `NONE`,
  read-only ODBC user `REPORTS`. About 60 sites (ICON plus legacy `OLD-*`) share it, and it is live production.
- **Style reference:** `Rinsed-Snowflake-Data-Client`. Mirror its layout, naming, docstrings and tests. Secondary
  references are `Sonnys-Data-API-Client` and `Sonnys-Backoffice-Wrapper` (Google-style docstrings, typed results, one
  docs guide per resource).

Everything marked **verified** below was checked against live data. That covers the DRB `CLAUDE.md`, the
`ODBC_Remote_Tester/docs/schema/*.md` mapping pass (2026-10-28), and the bounded probes run for this spec on
2026-10-30 (listed in section 12). Anything unverified is either left out of v1 or flagged.

---

## 1. Goals and non-goals

**Goals**

1. Wrap the queries WashU runs most often (sites, daily sales and car counts, item and category mix, tenders, ARM
   memberships, payroll timecards, labor statistics) in typed Python methods that return Pydantic models.
2. Encode SiteWatch's hidden business rules once, so callers can't get them wrong:
   - completed-only sales,
   - the car-count fact,
   - the tender sign flip,
   - `V_SalePasses` duplicate rows,
   - punch special codes,
   - Saturday-to-Friday pay weeks.
3. Make it **safe by construction** for a shared production database:
   - read-only transactions,
   - parameterized SQL,
   - mandatory date ranges with a cap on their length,
   - ICON sites by default,
   - a `SELECT`-only escape hatch that blocks the procedures that write data.
4. Keep personal data out of results unless a method explicitly asks for it.

**Non-goals (v1)**

- No writes of any kind, no `EXECUTE PROCEDURE`, and no data-changing procedures.
- No live "today" data. The central database lags (it currently ends 2026-10-15 for sales and 2026-10-16 for
  punches), so `sites.data_freshness()` exists to make that visible.
- No card or cardholder data: `V_SaleCharges` card fields, `V_SalePayments.MaskedPan` and `Expiration`.
- No GL-account reporting. `V_Item.GlAccount` is NULL on every item (verified), so SWSLS06's GL column cannot be
  produced. `V_GLAccount` is not joined.
- Heavy ARM procedures are left out of the typed API (see section 8.6): `Arm_Monthly_Current_Status`,
  `Arm_Summary_For_Date_Range` and `CustClubPlan_Balances`.

---

## 2. Package layout

```
src/sitewatch_data_client/
  __init__.py            # re-exports client, exceptions, all models, __version__
  _version.py            # __version__ = "0.1.0"
  _client.py             # SiteWatchClient (cached_property resources, connect/close/ctx mgr, _execute, query)
  _config.py             # SiteWatchConfig dataclass + load_config() from env
  _connection.py         # SiteWatchConnection: fdb connect, per-query read-only transaction, DataFrame result
  _exceptions.py         # SiteWatchError hierarchy
  _filters.py            # normalize_date / normalize_date_range / normalize_sites / placeholders()
  _query_builder.py      # every library SQL statement as a *_sql(...) builder returning (sql, params), as in Rinsed
  _safety.py             # assert_read_only_sql(): SELECT/WITH guard + blocked procedure list
  _constants.py          # ICON_SITE_IDS, ARM_PLAN_TYP, base types, ARM status names, punch codes, range limits
  py.typed
  resources/
    __init__.py
    _sites.py            # SitesResource
    _sales.py            # SalesResource
    _catalog.py          # CatalogResource
    _memberships.py      # MembershipsResource
    _labor.py            # LaborResource
    _timecards.py        # pure-Python punch classification (port of export_timecards.py), no SQL
  types/
    __init__.py
    _base.py             # SiteWatchModel(BaseModel) with ConfigDict(populate_by_name=True)
    _sites.py  _sales.py  _catalog.py  _memberships.py  _labor.py
tests/
  conftest.py            # mock_client: patch SiteWatchConfig.load_config, client._conn = MagicMock()
  test_config.py test_filters.py test_safety.py test_client.py
  test_sites.py test_sales.py test_catalog.py test_memberships.py test_labor.py test_timecards.py
  integration/           # marked @pytest.mark.integration, skipped unless SITEWATCH_* env is set
docs/
  index.md
  guides/ getting-started.md sites.md sales.md catalog.md memberships.md labor.md raw-queries.md
          error-handling.md safety.md
  api/ client.md models.md exceptions.md
README.md CHANGELOG.md CLAUDE.md mkdocs.yml pyproject.toml .env.example .gitignore LICENSE
```

Every resource module starts with `from __future__ import annotations`, as Rinsed's do. `SitesResource` and
`SalesResource` define a method named `list`, and without postponed annotations a later `-> list[Site]` in the same
class body would resolve `list` to that method and fail at import time.

All SQL text lives in `_query_builder.py` (Rinsed keeps its SQL there too). Resources call the builders and only map
rows. That gives the safety tests one module to scan for sensitive column names.

`pyproject.toml` has the following settings:

- build backend: hatchling
- `requires-python >=3.10`
- dependencies: `fdb`, `pydantic>=2.10,<3`, `pandas>=2,<3`
- extras: `dev = [pytest, ruff, python-dotenv]` and `docs = [mkdocs-material, mkdocstrings[python]]`
- wheel packages: `src/sitewatch_data_client`
- a `pytest` marker named `integration`

`.gitignore` must list `.env`, `.venv/`, `site/`, `__pycache__/`, `*.egg-info`, `dist/` and `exports/`.
`.env.example` uses placeholders only.

---

## 3. Configuration, connection and client

### 3.1 `SiteWatchConfig` (`_config.py`)

```python
@dataclass
class SiteWatchConfig:
    host: str
    database: str              # server-side path, e.g. E:\path\to\SiteWatch.FDB
    user: str
    password: str = field(repr=False)
    port: int = 3050
    fb_client: str | None = None   # absolute path to fbclient.dll (Firebird 2.5 client); None = fdb default lookup
    charset: str = "NONE"
    sql_dialect: int = 1

    @property
    def dsn(self) -> str: return f"{self.host}/{self.port}:{self.database}"

    @classmethod
    def load_config(cls) -> "SiteWatchConfig": ...
```

- **Required environment variables:** `SITEWATCH_HOST`, `SITEWATCH_DATABASE`, `SITEWATCH_USER` and
  `SITEWATCH_PASSWORD`.
- **Optional environment variables:** `SITEWATCH_PORT` (default 3050) and `SITEWATCH_FB_CLIENT`.
- A missing required variable raises `ConfigurationError`, listing the missing names but never the values.
- A non-integer `SITEWATCH_PORT` also raises `ConfigurationError`.
- The host is plain configuration. The owner has confirmed that queries also work over a host/IP other than the two
  documented ones, so neither the code nor the docs hard-code an address.
- `password` uses `repr=False`, so a printed config or a traceback doesn't leak it.
- The library does **not** load `.env` itself, the same as Rinsed. Callers use `python-dotenv` (a dev extra).
  README examples show `from dotenv import load_dotenv; load_dotenv()`.

`.env.example`:

```
SITEWATCH_HOST=your-query-server-host-or-ip
SITEWATCH_PORT=3050
SITEWATCH_DATABASE=E:\path\to\SiteWatch.FDB
SITEWATCH_USER=REPORTS
SITEWATCH_PASSWORD=change-me
SITEWATCH_FB_CLIENT=C:\path\to\fbclient25\bin\fbclient.dll
```

### 3.2 `SiteWatchConnection` (`_connection.py`)

- `connect()` runs:

  ```python
  fdb.connect(dsn=cfg.dsn, user=cfg.user, password=cfg.password, sql_dialect=1, charset="NONE",
              fb_library_name=cfg.fb_client)
  ```

  The `fb_library_name` argument is passed only when `fb_client` is set. Any failure becomes
  `ConnectionError("Failed to connect to SiteWatch at {host}:{port}: {e}")`, and the message never includes the password.
- `query(sql: str, params: Sequence | None = None) -> pd.DataFrame` works like this:
  - It opens **one read-only transaction per call** with
    `tr = con.trans(default_tpb=fdb.ISOLATION_LEVEL_READ_COMMITED_RO)`.
  - It runs `cur = tr.cursor(); cur.execute(sql, params or ())`.
  - Column names are lowercased.
  - String values are right-stripped. CHAR fields are space-padded, and charset `NONE` returns `str` decoded by fdb.
    If fdb returns `bytes`, decode them as `cp1252`.
  - The transaction is always closed with `tr.rollback()` in a `finally` block.
  - Any error becomes a `QueryError` whose message includes a preview of the first 100 characters of the SQL, never
    the parameters.
- `close()` is idempotent.
- Parameters use the fdb qmark style (`?`). Python `date` objects are converted to `datetime` at midnight (dialect-1
  `DATE` is a timestamp).

### 3.3 `SiteWatchClient` (`_client.py`)

```python
class SiteWatchClient:
    def __init__(self, host: str | None = None, database: str | None = None, user: str | None = None,
                 password: str | None = None, port: int = 3050, fb_client: str | None = None) -> None
    sites: SitesResource          # functools.cached_property
    sales: SalesResource
    catalog: CatalogResource
    memberships: MembershipsResource
    labor: LaborResource
    def connect(self) -> SiteWatchClient
    def close(self) -> None
    def __enter__(self) -> SiteWatchClient
    def __exit__(self, *exc) -> None
    def _execute(self, sql: str, params: Sequence | None = None) -> pd.DataFrame   # internal, trusted SQL
    def query(self, sql: str, params: Sequence | None = None, *, max_rows: int | None = 10000) -> pd.DataFrame
```

- **Credentials:** if every credential argument is None, the client uses `SiteWatchConfig.load_config()`. Otherwise
  host, database, user and password are all required, and a missing one raises `ConfigurationError`, the same as
  Rinsed.
- **`_execute`:** raises `ConnectionError("Not connected. Use connect() or a context manager.")` when
  `_conn is None`.
- **`query()`, the power-user escape hatch:**
  - It calls `assert_read_only_sql(sql)` (section 4) and then `_execute`.
  - `max_rows` caps the number of rows fetched (`fetchmany` loop) so an accidental scan can't flood memory.
    `None` removes the cap. When the cap cuts the result short, a WARNING is logged so truncation is never silent.
  - It returns a DataFrame.
  - Its docstring repeats the load rules: bound by `Site` plus a date range, use `FIRST n`, don't use `LIMIT`, and
    avoid `IN (SELECT ...)`.

### 3.4 Exceptions (`_exceptions.py`)

```
SiteWatchError(Exception)
 ├─ ConfigurationError
 ├─ ConnectionError
 ├─ QueryError
 └─ ValidationError          # bad dates, ranges, sites, args
     └─ UnsafeQueryError     # query() guard rejected the SQL
```

---

## 4. Safety rules (`_safety.py`, `_filters.py`, `_constants.py`)

1. **Read-only transaction on every statement.** This is the primary guard, and every query goes through
   `SiteWatchConnection.query`.
2. **`assert_read_only_sql(sql)`**, used by `query()` only. Resource SQL is library-owned.
   - Strip leading whitespace and `--` / `/* */` comments, then require the first keyword to be `SELECT` or `WITH`.
   - Reject a `;` followed by anything other than whitespace (one statement only).
   - Reject these keywords as whole words, case-insensitive, outside string literals: `INSERT UPDATE DELETE MERGE
     EXECUTE EXEC CREATE ALTER DROP RECREATE GRANT REVOKE SET COMMIT ROLLBACK` and `INTO`. `INTO` catches
     `SELECT ... INTO` in PSQL.
   - Reject any reference to the **blocked procedures**:

     ```
     DELETE_INVENTORY_DATA, DELETE_SALE_DATA, DELETE_SECURITY_DATA, DELETE_SPECIFIC_SALE, DELETE_TRAINING_SALES,
     GEN_OBJID, PREPAID_ACTIVATE_CODES, PREPAID_CREATE_TEMP_CODES, PREPAID_DEACTIVATE_CODES, PREPAID_SET_BALANCE,
     PREPAID_UPDATE_BALANCE, PREPAID_UPDATE_DAY_BALANCES, CUSTPPD_UPDATE_DAY_BALANCES, CUSTPROMO_UPDATE_BALANCE,
     CUSTPROMO_UPDATE_DAY_BALANCES
     ```

     Also reject any identifier matching `\w*_UPDATE_\w*` or `\w*_SET_\w*`, and the UDFs `f_GetDecryptedSSN` and
     `f_GetEncryptedSSN`.
   - Reject the sensitive employee columns `SSNUM`, `PWORD`, `SECURITYCODE`, `DRVLNUM`, `WEBPASSWORDHASH`,
     `WEBPASSWORDSALT`, and `SELECT *` / `x.*` against `V_EMPLOYEE` or `V_SITE` (`V_Site` holds email, SMTP and
     processor passwords).
   - Reject the `V_Site` credential columns by name too: `PASS`, `EMAILPASS`, `MPSPASSWORD`, `SECRETACCESSKEY`,
     `ACCESSKEYID` and `FACEBOOKAPPSECRET` (all in the `V_Site` column list in `catalog.json`). Without this,
     an explicit column list would get past the `SELECT *` check.
   - Reject the cardholder column `MASKEDPAN` and `SELECT *` / `x.*` against `V_SALEPAYMENTS` or `V_SALECHARGES`
     (non-goal: no card data).
   - Raise `UnsafeQueryError` with the reason.
   - This is a best-effort lexical guard and is documented as such. The read-only transaction stays the real safety
     net.
3. **Parameterize all user input.** Dates, codes and IDs always go in as `?` parameters. Site lists are validated as
   `int` and expanded as `?` placeholders through `placeholders(n) -> "?, ?, ?"`. No caller-supplied value is ever
   formatted into SQL text. The only formatted fragments are library constants and placeholder strings.
4. **Mandatory, capped date ranges.** Every history method takes `start` and `end` (inclusive) and validates them with
   `normalize_date_range(start, end, max_days)`:
   - It accepts a `date`, a `datetime` (the time part is dropped) or an ISO `YYYY-MM-DD` string.
   - It requires `start <= end`.
   - It requires `(end - start).days + 1 <= max_days`.
   - It returns `(start_dt, end_exclusive_dt)` for the half-open filter `LogDate >= ? AND LogDate < ?`.
   - `LogDate` and `PunchDate` are midnight timestamps, so half-open is exact.
   - Limits live in `_constants.RANGE_LIMITS` and are listed per method below. An oversize range raises
     `ValidationError`, with a message that says to split the range or use `query()`.
5. **Default sites.** `sites=None` means `ICON_SITE_IDS = (5, 13, 18, 20, 24, 25)`, the car washes. Office 99 and
   query server 100 have no sales or punches; they are exposed as `ICON_ALL_SITE_IDS` for anyone who wants them.
   - `normalize_sites(sites)` accepts an `int`, an iterable of ints, or None.
   - It rejects bools, non-ints, values that are 0 or less, and an empty list. Duplicates are not an error: they
     are removed silently and the result is sorted.
   - Single-site methods take `site: int` (required).
6. **Load etiquette.**
   - The client never runs queries in parallel.
   - Methods issue at most a few statements each.
   - No `IN (SELECT ...)`: use derived tables plus `JOIN` (a correlated scan is a known 180-second-plus trap).
   - No unbounded `COUNT(*)` on history views.
7. **Privacy.**
   - `V_Employee` is selected by explicit column list only, never with the sensitive columns.
   - Customer names, phones, addresses, emails and plates come back only from `memberships.new_members(...,
     include_contact=True)`.
   - Sales results expose `customer_id` and `customer_code_id` (internal ObjIDs), never the plate text.
   - Docs, tests and fixtures use made-up values only.

---

## 5. Shared conventions

- **Base model:** `SiteWatchModel(BaseModel)` with `model_config = ConfigDict(populate_by_name=True)`. Every result
  model inherits from it.
- **Types:**
  - money is `float`, rounded to 2 dp in Python after the SQL casts it with `CAST(... AS NUMERIC(12,2))`. The
    Python rounding is **mandatory**, not cosmetic. In dialect 1 a `NUMERIC` with precision above 9 is stored as a
    double, so the cast does not remove float noise: live, `CAST(SUM(...) AS NUMERIC(12,2))` returned
    `2112.090000000001` and `1837.0200000000011`. `NUMERIC(9,2)` comes back from fdb as `Decimal`, which the
    mapper converts to `float`;
  - dates are `datetime.date`;
  - timestamps are naive `datetime.datetime` in site-local time (US Central at ICON; SiteWatch stores local
    wall-clock time with no offset). They are never converted, because no UTC offset is stored. On DST change
    nights a wall-clock difference is 1 hour off from elapsed time (section 9.0);
  - IDs are `int`.
- **Text:** trimmed. Empty strings become None.
- **Site display:** every row-level model that carries `site_id` also carries `site_code` (for example
  `ICONTN-030`), taken from `V_Site`. The site code is **not** the site ID (Site 13 is ICONTN-015).
- **Ordering:** results are ordered deterministically: site, date, then a natural key.
- **Docstrings:** Google style, with `Args`/`Returns`/`Raises`/`Examples`, following Rinsed `pos.py`. Each docstring
  states the business rule and the performance note.
- **Parsing:** each resource maps DataFrame rows to models through a small `_row_to_model` helper. NaN and None
  become None.

---

## 6. `client.sites`: SitesResource

### 6.1 `list(sites: Iterable[int] | int | None = None, *, include_office: bool = False) -> list[Site]`

- **What it returns:** ICON car-wash sites by default. `include_office=True` adds 99 and 100. Passing explicit
  `sites` returns exactly those sites, which can include legacy `OLD-*` sites.
- **Join:** `V_System.Site = V_Site.ID` is 1:1 (59/59, verified).
- **Source of each field:** `V_Site` has the code and name. `V_System` has the address and phone. `V_System.Name` is
  always "System" and must not be used as a name.

```sql
SELECT st.ID, st.Code, st.SiteName, st.Enabled,
       y.Address, y.Address2, y.City, y.State, y.Zip, y.Phone
FROM V_Site st
LEFT JOIN V_System y ON y.Site = st.ID
WHERE st.ID IN (?, ?, ...)
ORDER BY st.ID
```

Never use `SELECT *` on `V_Site`, because it holds credentials.

```python
class Site(SiteWatchModel):
    site_id: int
    code: str                 # ICONTN-030
    name: str                 # V_Site.SiteName, trimmed ("Icon Car Wash- Nashville")
    enabled: bool             # V_Site.Enabled == 1 (1 Enabled, 2 Disabled)
    address: str | None
    address2: str | None
    city: str | None          # as typed in V_System (e.g. Goodlettsville's row says "Madison")
    state: str | None
    zip: str | None
    phone: str | None
```

### 6.2 `get(site_id: int) -> Site`

This calls `list([site_id])[0]`. An unknown ID raises `ValidationError`.

### 6.3 `data_freshness(sites=None) -> list[SiteFreshness]`

Use this before trusting any recent window. It runs two cheap probes per site, one at a time. Each is a `MAX` with a
date floor (`floor = today - 400 days`, passed as a parameter), never a `GROUP BY` over all history:

```sql
SELECT MAX(s.LogDate) FROM V_Sale s WHERE s.Site = ? AND s.LogDate >= ?
SELECT MAX(p.PunchDate) FROM V_PunchInterval p WHERE p.Site = ? AND p.PunchDate >= ?
```

**Measured 2026-10-30 at site 24:** the `ORDER BY ... DESC` form on `V_SaleStats` (the probe the DRB `CLAUDE.md`
suggests) took **19.7 s**. `MAX(LogDate)` on `V_Sale` with the floor took **0.9 s**, and both returned 2026-10-15.
For punches, both forms were under 0.5 s. A site with nothing inside the floor reports `None`.

It also runs one replication query:

```sql
SELECT r.Site, r.PacketAppliedDate FROM V_Replication r WHERE r.Site IN (?, ...)
```

```python
class SiteFreshness(SiteWatchModel):
    site_id: int
    site_code: str
    latest_sale_date: date | None
    latest_punch_date: date | None
    replication_applied_at: datetime | None   # V_Replication.PacketAppliedDate (None if no row)
```

The floor is computed in Python, so the SQL never depends on the server's `TODAY`, and unit tests can pin it.

---

## 7. `client.sales`: SalesResource

### 7.0 Business rules (verified)

These rules apply to every method in `client.sales`.

- **Completed sale:** `f_IsCompleted(s.Status) = 1`. That means the codes `C:P` (paid, nothing to do, including ARM
  recharges and XPT cash events) and `C:PW` (paid and washed).
  - `I:P-W` (paid, wash pending), `E:-P` (failed recharge attempt, LogDate NULL) and `Notes` (ARM decline or expire
    events) are excluded.
  - Site 24 on 2026-10-14 had C:P 57, C:PW 164, I:P-W 8 and Notes 217. The Notes rows carry a nonzero `Total`
    ($6,570.72) that must **not** be counted.
  - A sixth status, `PendAppr`, also occurs. At site 24 over 2026-10-01..14 there was 1 such sale, with LogDate set
    and a $0 total. Both `f_IsCompleted` and `f_IsIncomplete` return 0 for it, so it is excluded everywhere.
- **Paid but wash pending (`I:P-W`):** these sales are **paid**. Their tender lines balance the total: at site 24
  over 2026-10-01..14 there were 66 such sales, $109.98, with base type 180 summing to -109.98. `f_IsIncomplete` = 1
  identifies them. SiteWatch's own car count (below) and the ARM roll-up (8.0) both **include** them. The DRB rule
  "revenue = completed only" excludes them. To keep the figures reconcilable, `daily_summary` reports them in
  separate `pending_wash_*` fields and never folds them silently into `sales_total`. "Paid" in this spec means
  `LogDate` in range AND `(f_IsCompleted(Status) = 1 OR f_IsIncomplete(Status) = 1)`. An unpaid sale has a NULL
  `LogDate`, so the date filter already removes it.
- **Business date:** `LogDate` (paid date, a midnight timestamp). Filter `LogDate >= ? AND LogDate < ?`.
- **Paid timestamp:** `LogDate + LogTime / 14400.0`, which equals `V_SaleStats.TStamp` (verified). `Created` is when
  the sale was started.
- **Sale total:** `V_Sale.Total` is the sum of the non-tender lines (657/657 and 41/41 verified), and all lines of a
  completed sale sum to 0.
  - `Total` includes XPT cash-acceptor **deposit** lines (item base type 170). These are cash movements, not revenue
    (2026-10-14 at site 24: one "XPTCASH" sale, Total -76.00).
  - The API therefore reports both `sales_total`, which is SiteWatch's raw number, and `revenue = sales_total -
    deposits` (deposits are base type 170 lines).
  - `Total` also includes **paid-out** lines (base type 160). At ICON these are ARM refunds: "ARM Refund"
    (category Paidouts) and the per-plan "... Rfnd" items (category ARM Plans Terminated). Site 24 had -35.93 on
    2026-10-14 and -529.29 over 2026-10-01..14 (completed sales). They are real money returned to customers, so they stay inside
    `revenue` as a net figure. They are also reported separately as `paidouts`, so gross and net can both be
    derived. No tax (base type 190) lines occurred at site 24 in that window.
- **Car count:** SiteWatch's own car-count statistic is the `V_SaleFacts` fact named **`TotalCarCount`** in **profit
  center 1** (Wash). Verified on 2026-10-30:
  - At site 24 on 2026-10-14 it equals the number of paid sales that contain a **Basic Washes** item (report category
    branch `001001001`): 172 = 164 C:PW + 8 I:P-W, with no C:P or Notes sales. The value is 1 per sale.
  - The PC 0 and PC 1 values are identical for this fact, and `CountedCarCount` equals `TotalCarCount` in every
    sampled day (site 5 on 10-12 and 10-13: 184 and 191; site 24 on 10-14: 172).
  - Car count therefore **includes** paid sales whose wash is still pending (`I:P-W`). It covers ARM member washes
    ($0 sales) and retail washes alike, and it **excludes** recharges, plan sales without a wash, and XPT events.
  - The fact is looked up **by name** (`JOIN V_Fact f ON f.ObjID = sf.Fact AND f.Name = 'TotalCarCount'`), not by its
    ObjID (54200 here), and filtered with `sf.ProfitCenter = 1`. Filtering a single profit center avoids double
    counting (PC 0 and 1 carry different fact sets).
  - The **wash count** from line items (completed sales only) is the number of Basic Washes package lines. It equals
    car count minus pending-wash sales (164 on 2026-10-14) and is available through
    `item_sales(report_category="Basic Washes")`.
- **Retail vs member car count is not split in v1.** The candidate signals don't reconcile on the sampled day:
  - ARM "Redeemed" discount lines: 109;
  - `V_ARM_Monthly_Activity.WashUsed`: 117;
  - "ARM Redeemer Pkg" header lines: 108;
  - employee-plan discounts are another overlapping case.

  This split is listed as future work (section 11).
- **Tenders:** lines whose item base type is 180, with `Amt` negative for money received. Tender amount is
  `-SUM(Amt)`.
  - Tender lines are named by `V_Item.Name` and `V_ItemRptCategory`, **never** through `V_Tender`, because that join
    is N:M: Visa and MC share item 4744.
  - Cash at XPT kiosks appears as separate `CASH` and `CHANGE` lines (and `Cash/Change` at 0), so cash is reliable
    only when summed at report-category level.
- **Edited sales / ActualSaleID:** on ICON data `ActualSaleID` is set on ARM recharge chains (C:P, E:-P, Notes) and is
  NULL on every C:PW sale (verified). It is **not** an "original sale" filter. The completed-only rule already
  removes the E:-P and Notes chain members, so no extra ActualSaleID filter is applied. The value is exposed raw on
  `Sale.actual_sale_id`.
  - Re-checked 2026-10-30 for double counting. At site 24, 2026-10-01..14, 836 completed sales carry an
    ActualSaleID. All are `C:P` recharges. 835 point to a sale dated **before** the window, and only 1 ($48.95)
    points to a completed sale inside it. That one is a recharge chained to its join sale, not an edit. Within a
    window, no completed sale is re-counted through the chain.
- **Line amounts:** use `COALESCE(si.Amt, 0)`, because note and header lines have a NULL `Amt`. Package member lines
  have `Leader` set to the leader's `Seq`.
- **Item names may be personal names.** "Wash Notes" lines can carry an employee or customer name as the item name.
  The docs warn about this. It can't be filtered reliably, so item-level results should be treated as internal.

### 7.1 `daily_summary(start, end, sites=None) -> list[DailySales]`

- **Range limit:** 31 days.
- **Grain:** one row per site per business day that has any sale.
- **Statements:** three, merged in Python on `(site, log_date)`.

(a) Header totals, split into completed and paid-pending (verified live, 0.5 s for one site-day):

```sql
SELECT s.Site, s.LogDate, f_IsCompleted(s.Status) AS IsCompleted,
       COUNT(*) AS Sales,
       CAST(SUM(s.Total) AS NUMERIC(12,2)) AS SalesTotal
FROM V_Sale s
WHERE s.Site IN (?, ...) AND s.LogDate >= ? AND s.LogDate < ?
  AND (f_IsCompleted(s.Status) = 1 OR f_IsIncomplete(s.Status) = 1)
GROUP BY s.Site, s.LogDate, 3
```

`IsCompleted = 1` rows feed `completed_sales` and `sales_total`. `IsCompleted = 0` rows (I:P-W) feed
`pending_wash_sales` and `pending_wash_total`.

(b) Car count:

```sql
SELECT s.Site, s.LogDate, SUM(sf.Data) AS CarCount
FROM V_Sale s
JOIN V_SaleFacts sf ON sf.Site = s.Site AND sf.SaleID = s.ObjID
JOIN V_Fact f ON f.ObjID = sf.Fact
WHERE s.Site IN (?, ...) AND s.LogDate >= ? AND s.LogDate < ?
  AND sf.ProfitCenter = 1 AND f.Name = 'TotalCarCount'
GROUP BY s.Site, s.LogDate
```

`SUM(sf.Data)` is a double. Convert it with `int(round(x))` in Python, not with `CAST AS INTEGER`.

(c) Discounts, paid-outs, deposits and tenders by base type:

```sql
SELECT s.Site, s.LogDate, t.BaseType, CAST(SUM(COALESCE(si.Amt, 0)) AS NUMERIC(12,2)) AS Amt
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
WHERE s.Site IN (?, ...) AND s.LogDate >= ? AND s.LogDate < ?
  AND f_IsCompleted(s.Status) = 1 AND t.BaseType IN (150, 160, 170, 180)
GROUP BY s.Site, s.LogDate, t.BaseType
```

```python
class DailySales(SiteWatchModel):
    site_id: int
    site_code: str
    log_date: date
    completed_sales: int      # completed sale headers (includes ARM recharges and $0 member washes)
    car_count: int            # TotalCarCount fact, PC 1 (includes paid-not-yet-washed)
    sales_total: float        # SUM(V_Sale.Total) of completed sales, SiteWatch's raw figure
    deposits: float           # base type 170 line total (XPT deposits, normally <= 0)
    revenue: float            # sales_total - deposits (net of paidouts/refunds)
    paidouts: float           # base type 160 line total (ARM refunds; <= 0), already inside revenue
    discounts: float          # base type 150 line total (ARM redemptions, LPM/employee discounts; <= 0)
    tendered: float           # -SUM(base type 180 Amt); equals sales_total on balanced days
    pending_wash_sales: int   # I:P-W sales: paid, wash pending; counted in car_count, NOT in completed_sales
    pending_wash_total: float # SUM(V_Sale.Total) of those sales; NOT in sales_total or revenue
```

`totals(start, end, sites=None) -> SalesTotals` (also limited to 31 days) is a convenience wrapper that sums
`daily_summary` in Python. It mirrors Rinsed's `total` plus `by_location` shape:

```python
class SiteMetric(SiteWatchModel):
    site_id: int
    site_code: str
    value: float

class SalesTotals(SiteWatchModel):
    period_start: date
    period_end: date
    completed_sales: int
    car_count: int
    sales_total: float
    revenue: float
    paidouts: float
    pending_wash_sales: int
    car_count_by_site: list[SiteMetric]
    revenue_by_site: list[SiteMetric]
```

**Expected tie-out for integration tests:** site 24, 2026-10-14 gives:

| Field | Value |
|---|---|
| completed_sales | 221 |
| car_count | 172 |
| sales_total | 2112.09 |
| deposits | -76.00 |
| revenue | 2188.09 |
| paidouts | -35.93 |
| tendered | 2112.09 |
| pending_wash_sales | 8 |
| pending_wash_total | 0.00 |

These are aggregate operational figures, not personal data, so they are fine in an integration test. They will drift
only if the historical data is edited.

### 7.2 `hourly_summary(start, end, sites=None) -> list[HourlySales]`

- **Range limit:** 14 days.
- **Hour source:** `V_SaleStats.Hr`, never a CAST of LogTime, because dialect-1 CAST rounds.

```sql
SELECT ss.Site, ss.LogDate, ss.Hr, COUNT(*) AS CompletedSales, CAST(SUM(s.Total) AS NUMERIC(12,2)) AS SalesTotal
FROM V_SaleStats ss
JOIN V_Sale s ON s.Site = ss.Site AND s.ObjID = ss.SaleID
WHERE ss.Site IN (?, ...) AND ss.LogDate >= ? AND ss.LogDate < ? AND f_IsCompleted(ss.Status) = 1
GROUP BY ss.Site, ss.LogDate, ss.Hr
```

Car count per hour comes from the same fact join as 7.1(b), grouped by `ss.Hr` through `V_SaleStats`. It was
verified live at site 24 on 2026-10-14 in 0.2 s, and the hourly values sum to 172, the daily car count:

```sql
SELECT ss.Site, ss.LogDate, ss.Hr, SUM(sf.Data) AS CarCount
FROM V_SaleStats ss
JOIN V_SaleFacts sf ON sf.Site = ss.Site AND sf.SaleID = ss.SaleID
JOIN V_Fact f ON f.ObjID = sf.Fact
WHERE ss.Site IN (?, ...) AND ss.LogDate >= ? AND ss.LogDate < ?
  AND sf.ProfitCenter = 1 AND f.Name = 'TotalCarCount'
GROUP BY ss.Site, ss.LogDate, ss.Hr
```

```python
class HourlySales(SiteWatchModel):
    site_id: int
    site_code: str
    log_date: date
    hour: int                 # 0-23, paid hour
    completed_sales: int
    car_count: int
    sales_total: float
```

The docs note that hour 6 is dominated by automatic ARM recharges (around 06:20, terminal "Automatic Recharge"),
which inflate `completed_sales` and `sales_total`, though not `car_count`.

### 7.3 `item_sales(start, end, sites=None, *, group_by="item", report_category=None, include_zero=False) -> list[ItemSales]`

- **Range limit:** 31 days.
- **Port of:** SWSLS06 (minus GL) and the "sales mix by category" query.
- **`group_by`:** one of `"item"`, `"category"` or `"base_type"`. Rows are always also split by site and day.
- **`report_category`:** optional. It is matched by **name** on the category itself or any ancestor, so
  `"Wash"` covers the whole Wash tree. The library resolves the name to its `Branch` with a catalog lookup (7.3a),
  then filters `rc.Branch STARTING WITH ?`. A name matching no category raises `ValidationError`.
- **`include_zero`:** False drops rows where both amount and quantity are 0 (headers, placeholders).

```sql
-- group_by = "item"
SELECT s.Site, s.LogDate, i.ObjID AS ItemID, i.Name AS ItemName, rc.Name AS Category, rc.Branch,
       t.BaseType, COUNT(*) AS Lines, CAST(SUM(COALESCE(si.Qty, 0)) AS NUMERIC(12,2)) AS Qty,
       CAST(SUM(COALESCE(si.Amt, 0)) AS NUMERIC(12,2)) AS Amount
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE s.Site IN (?, ...) AND s.LogDate >= ? AND s.LogDate < ? AND f_IsCompleted(s.Status) = 1
  [AND rc.Branch STARTING WITH ?]
GROUP BY s.Site, s.LogDate, i.ObjID, i.Name, rc.Name, rc.Branch, t.BaseType
```

The `"category"` and `"base_type"` groupings drop the item columns (and the category columns for `base_type`).

The joins `V_SaleItems → V_Item → V_ItemType / V_ItemRptCategory` are 0-orphan (verified on 4,848 items).
`V_Item.Enabled` is **not** filtered, because disabled items still appear on historical sales.

(7.3a) Branch lookup:

```sql
SELECT rc.Branch FROM V_ItemRptCategory rc WHERE UPPER(TRIM(rc.Name)) = UPPER(?)
```

The parameter is `name.strip()`. Category names are unique case-insensitively: a `GROUP BY UPPER(TRIM(Name))
HAVING COUNT(*) > 1` check returned 0 rows on 2026-10-30. The code still raises `ValidationError` if the lookup
returns more than one row, rather than picking one.

```python
class ItemSales(SiteWatchModel):
    site_id: int
    site_code: str
    log_date: date
    item_id: int | None          # None unless group_by="item"
    item_name: str | None
    category: str | None         # V_ItemRptCategory.Name (None when group_by="base_type")
    category_branch: str | None  # e.g. "001001001" (Categories > Wash > Basic Washes)
    base_type: int               # 100 Note ... 120 Service, 140 Prepaid, 150 CouponDisc, 160 Paidout, 170 Deposit, 180 Tender, 190 Tax, 200 Flag
    base_type_name: str          # from _constants.BASE_TYPES (avoids a lookup join)
    lines: int
    quantity: float
    amount: float                # signed as stored: discounts and tenders are negative
```

The docs give recipes for common views:

- wash package mix: `report_category="Basic Washes"` with `group_by="item"`;
- membership revenue: the `"ARM Plans Sold"` and `"ARM Plans Recharged"` categories;
- discounts: `base_type == 150`.

### 7.4 `tenders(start, end, sites=None, *, by_item: bool = False) -> list[TenderTotal]`

- **Range limit:** 31 days.
- **Scope:** completed sales, base type 180 lines.

```sql
SELECT s.Site, s.LogDate, rc.Name AS Category, rc.Branch, [i.Name AS ItemName,]
       COUNT(*) AS Lines, CAST(-SUM(COALESCE(si.Amt, 0)) AS NUMERIC(12,2)) AS Amount
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE s.Site IN (?, ...) AND s.LogDate >= ? AND s.LogDate < ? AND f_IsCompleted(s.Status) = 1
  AND t.BaseType = 180
GROUP BY s.Site, s.LogDate, rc.Name, rc.Branch [, i.ObjID, i.Name]
```

With `by_item=True` the grouping includes `i.ObjID`, so two tender items that share a display name stay separate.
`ItemSales` uses the same rule.

```python
class TenderTotal(SiteWatchModel):
    site_id: int
    site_code: str
    log_date: date
    category: str             # "Credit Card", "Cash", "House Accounts", "XPT Cash Add/Remove", ...
    category_branch: str
    tender: str | None        # item name ("Visa / MC", "American Express") when by_item=True
    lines: int
    amount: float             # money received (positive)
```

Verified at site 24 on 2026-10-14: Credit Card 2,092.09 (Visa/MC 1,893.24, Amex 186.85, Discover 12.00), House
Accounts 28.00, XPT CASH 19.00 less CHANGE 27.00. The total equals `sales_total` (2,112.09).

The documented caveat: `Lines` is a count of tender lines, not of customers or transactions (a sale can have several
tender lines). XPT cash also appears as the CASH/CHANGE pair, so the cash figure is reliable only at category level.

### 7.5 `list(start, end, site, *, include_items=True, include_incomplete=False) -> list[Sale]`

- **Range limit:** 3 days. It's row-level data for one site, so it is capped tightly.
- **Port of:** SWSLS05 (long format).
- **Defaults:** completed sales only. `include_incomplete=True` adds every other status for diagnostics.
- **Statements:** two.

(a) Headers. This ran in about 6 s for one site-day:

```sql
SELECT s.Site, s.ObjID, f_SaleScr(s.ObjID) AS ReceiptNumber, s.Code, f_StatusCode(s.Status) AS StatusCode,
       f_IsCompleted(s.Status) AS IsCompleted, s.Created, s.LogDate,
       s.LogDate + s.LogTime / 14400.0 AS PaidAt, ss.Hr, s.Shift,
       s.Terminal AS TerminalID, t.Name AS TerminalName,
       s.CustomerCode AS CustomerCodeID, cc.Customer AS CustomerID,
       s.ActualSaleID, CAST(s.Total AS NUMERIC(12,2)) AS Total
FROM V_Sale s
JOIN V_SaleStats ss ON ss.Site = s.Site AND ss.SaleID = s.ObjID
LEFT JOIN V_Terminal t ON t.ObjID = s.Terminal              -- N:0..1, Terminal can be NULL
LEFT JOIN V_CustomerCode cc ON cc.ObjID = s.CustomerCode    -- N:0..1
WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ?
  [AND f_IsCompleted(s.Status) = 1]
ORDER BY s.LogDate, s.LogTime, s.ObjID
```

`E:-P` sales have a NULL LogDate, so the date filter excludes them anyway. The docs say so.

(b) Lines, if `include_items` is set. The same WHERE clause, joined to the lines and grouped in Python by `ObjID`:

```sql
SELECT s.ObjID AS SaleID, si.Seq, si.Leader, si.Item, i.Name AS ItemName, rc.Name AS Category, t.BaseType,
       si.Qty, si.Val, CAST(si.Amt AS NUMERIC(12,2)) AS Amt
FROM V_Sale s
JOIN V_SaleItems si ON si.Site = s.Site AND si.SaleID = s.ObjID
JOIN V_Item i ON i.ObjID = si.Item
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE <same as (a)>
ORDER BY s.ObjID, si.Seq
```

The `si.Note` column is **not** selected, because it is free text that may hold personal data.

```python
class SaleLine(SiteWatchModel):
    seq: int
    leader_seq: int | None    # package leader line Seq
    item_id: int
    item_name: str
    category: str
    base_type: int
    is_tender: bool           # base_type == 180
    quantity: float | None
    unit_value: float | None  # V_SaleItems.Val
    amount: float | None      # None on note/header lines

class Sale(SiteWatchModel):
    site_id: int
    sale_id: int              # V_Sale.ObjID (internal; NOT chronological)
    receipt_number: str       # f_SaleScr(ObjID) as text
    code: str | None
    status_code: str          # C:P, C:PW, I:P-W, Notes, PendAppr (E:-P never has a LogDate)
    is_completed: bool
    created_at: datetime | None
    log_date: date | None
    paid_at: datetime | None
    hour: int | None
    shift: int | None
    terminal_id: int | None
    terminal_name: str | None # e.g. "Automatic Recharge", "XPT1"
    customer_code_id: int | None
    customer_id: int | None
    actual_sale_id: int | None
    total: float
    items: list[SaleLine] = []
```

### 7.6 `status_mix(start, end, sites=None) -> list[StatusCount]`

- **Range limit:** 14 days.
- **Purpose:** diagnostics and reconciliation, because it shows what the completed-only filter removes.

```sql
SELECT s.Site, s.LogDate, f_StatusCode(s.Status) AS StatusCode, f_IsCompleted(s.Status) AS IsCompleted,
       COUNT(*) AS Sales, CAST(SUM(s.Total) AS NUMERIC(12,2)) AS Total
FROM V_Sale s
WHERE s.Site IN (?, ...) AND s.LogDate >= ? AND s.LogDate < ?
GROUP BY s.Site, s.LogDate, 3, 4
```

The mixed name and ordinal `GROUP BY` was verified live (site 24, 2026-10-14, 0.5 s). The docs state that `E:-P`
attempts never appear here, because their `LogDate` is NULL. The method shows what the completed filter removes
from **paid-date** sales only.

```python
class StatusCount(SiteWatchModel):
    site_id: int
    site_code: str
    log_date: date
    status_code: str
    is_completed: bool
    sales: int
    total: float
```

**Left out of v1:** house-account charges (SWSLS07). They run through `V_SaleCharges`, which is slow: about 6 s per
site-day, and over 2 minutes for 7 days. A guide recipe for `query()` covers them instead.

---

## 8. `client.memberships`: MembershipsResource (ARM)

### 8.0 Business rules (verified)

- **ARM plans** are `V_PlanType.Typ = 110` (ARM Monthly Pass), and every ICON `V_SalePasses` row is Typ 110.
  `Enabled` is 1 for enabled and 2 for disabled.
- **`V_SalePasses`** has no date column, so it is always bounded through `V_Sale` on `Site + SaleID` plus `LogDate`.
  `(Site, SaleID, Seq)` is not unique (Seq can tie at 0), so methods that count events per sale use
  `COUNT(DISTINCT ...)` or the daily roll-up.
- **Status codes** are in `_constants.ARM_STATUS`, with server-side decoding by `f_ArmStatusName`:

  | Code | Meaning |
  |---|---|
  | 0 | Used |
  | 1 | Joined |
  | 2 | Transfer In |
  | 3 | Renewed |
  | 6 | Resumed |
  | 7 | Discontinuing |
  | 21 | Expired |
  | 27 | Discontinued |
  | 29 | Terminated |
  | 30 | Transfer Out |
  | 40 | Suspended |
  | 110 | CC Expired |
  | 120 | CC Declined |
  | 130 | Recharge Problem |
  | 140 | CC Information Required |
  | 150 | CC Approved |
  | 200 | Credit Card Changed |
  | 201 | Join Date Changed |

- **`V_ARM_Monthly_Activity`** is **daily**, despite its name. Its grain is (Site, LogDate, Leader, PlanType), with
  one row per leader *who had activity that day*, so its row count is **not** an active-member count. Its counts
  equal that day's `V_SalePasses` Joined, Renewed and Used counts (verified at site 13 on 2026-08-03).
  - Re-verified at site 24 on 2026-10-14: SaleCount 8 equals the "ARM Plans Sold" lines, and RechargeCount 52 equals
    the "ARM Plans Recharged" lines (2+1+21+9+12+7).
  - Re-verified over two weeks (site 24, 2026-10-01..14), against `V_SalePasses` split by sale status:
    - SaleCount 60 = Status 1 on C:PW (58) + I:P-W (2);
    - WashUsed 1,686 = Status 0 on C:PW (1,627) + I:P-W (59);
    - RechargeCount 837 = Status 3 on C:P (837);
    - Declined 2,030 = Status 120 on Notes;
    - Expired 42 = Status 21 on Notes;
    - Discontinued 52 = Status 27 on Notes.

    So the roll-up counts **paid** sales, including I:P-W, not completed-only. Methods that count from
    `V_SalePasses` directly must use the same paid rule (7.0) to reconcile with `activity()`.
  - Seq duplicates were checked on the same window. Within each (sale status, pass status) group, row count equals
    distinct SaleID count (for example 1,627/1,627 for Used), so they don't inflate use or join counts there. They
    remain possible across statuses on decline sales (120 plus 140).

### 8.1 `plans(*, include_disabled: bool = False) -> list[MembershipPlan]`

```sql
SELECT pt.ObjID, pt.Name, pt.Enabled, pt.ExpirationAge, pt.RechargePlan, pt.Modified
FROM V_PlanType pt
WHERE pt.Typ = 110 [AND pt.Enabled = 1]
ORDER BY pt.Name
```

```python
class MembershipPlan(SiteWatchModel):
    plan_type_id: int
    name: str                  # "Unlimited Bronze", "Unlimited Silv 2022", "Family ARM $75", ...
    enabled: bool
    expiration_age_days: int | None   # 0 = none, 9999 = effectively never
    recharge_plan_id: int | None      # forced migration target (rarely set)
    modified_at: datetime | None
```

Plan types have no Site column and are global, so legacy plans appear too. Prices are not in `V_PlanType`; a join's
amount comes from the sale (8.3).

### 8.2 `activity(start, end, sites=None, *, by_plan: bool = True) -> list[MembershipActivity]`

- **Range limit:** 93 days.
- **Source:** the daily roll-up. This is the cheap, verified source for joins, recharges and churn events. Measured:
  14 days at all 6 ICON sites (33,367 roll-up rows) aggregated in 1.1 s.

```sql
SELECT a.Site, a.LogDate, [a.PlanType, pt.Name AS PlanName,]
       SUM(a.SaleCount) AS Joins, CAST(SUM(a.SaleAmount) AS NUMERIC(12,2)) AS JoinAmount,
       SUM(a.RechargeCount) AS Recharges, CAST(SUM(a.RechargeAmount) AS NUMERIC(12,2)) AS RechargeAmount,
       SUM(a.UpsellCount) AS Upsells, CAST(SUM(a.UpsellAmount) AS NUMERIC(12,2)) AS UpsellAmount,
       SUM(a.WashUsed) AS WashesUsed,
       SUM(a.TransferInCount) AS TransfersIn, SUM(a.TransferOutCount) AS TransfersOut,
       SUM(a.SuspendedCount) AS Suspended, SUM(a.ResumedCount) AS Resumed,
       SUM(a.TerminatedCount) AS Terminated, CAST(SUM(a.TerminatedAmount) AS NUMERIC(12,2)) AS TerminatedAmount,
       SUM(a.Expired) AS Expired, SUM(a.Discontinuing) AS Discontinuing, SUM(a.Discontinued) AS Discontinued,
       SUM(a.Declined) AS Declined, SUM(a.CardExpiring) AS CardExpiring, SUM(a.RechargeProblem) AS RechargeProblems
FROM V_ARM_Monthly_Activity a
[JOIN V_PlanType pt ON pt.ObjID = a.PlanType]
WHERE a.Site IN (?, ...) AND a.LogDate >= ? AND a.LogDate < ?
GROUP BY a.Site, a.LogDate [, a.PlanType, pt.Name]
```

```python
class MembershipActivity(SiteWatchModel):
    site_id: int
    site_code: str
    log_date: date
    plan_type_id: int | None   # None when by_plan=False
    plan_name: str | None
    joins: int
    join_amount: float
    recharges: int
    recharge_amount: float
    upsells: int
    upsell_amount: float
    washes_used: int
    transfers_in: int
    transfers_out: int
    suspended: int
    resumed: int
    terminated: int
    terminated_amount: float
    expired: int
    discontinuing: int
    discontinued: int
    declined: int
    card_expiring: int
    recharge_problems: int
```

`activity_totals(start, end, sites=None) -> MembershipActivityTotals` sums this in Python. It has the fields
`period_start`, `period_end`, `joins`, `recharges`, `recharge_amount`, `terminated`, `expired`, `discontinued`,
`declined` and `joins_by_site: list[SiteMetric]`.

### 8.3 `new_members(start, end, sites=None, *, include_contact: bool = False, include_recharge_site: bool = False, as_of: date | None = None) -> list[NewMember]`

- **Range limit:** 31 days.
- **Port of:** `export_members.ps1`, with these changes:
  - a site filter;
  - parameterized dates;
  - no card fields;
  - PII only when explicitly requested.
- **Who is included:** a customer is a new member when they have a `V_SalePasses` row with `Status = 1` (Joined) on a
  **paid** sale (completed or I:P-W, section 7.0) at one of `sites` with `LogDate` in the window. Their join date
  is the first such date.
  - This deliberately differs from `export_members.ps1`, which used completed-only. The paid rule makes
    `len(new_members(...))` reconcile with `activity_totals(...).joins`.
  - Evidence (site 24, 2026-10-01..14): 58 completed joins plus 2 on I:P-W sales give 60, which equals
    `SUM(SaleCount)`.
- **Current state:** comes from `Arm_SalePass_Member(customer, as_of)`. It returns exactly one row per member
  (verified for 12 new members and 113 members who washed on 2026-10-14). `as_of=None` passes NULL, meaning "now"
  on the server.
  - **`as_of` is end-of-day.** A `date` is sent as `datetime.combine(as_of, time(23, 59, 59))`, never as midnight.
  - Evidence: with `'2026-10-14'` (midnight) passed, 7 of the 8 customers who joined on 10-14 came back with no
    state (NULL PassStatus), because they weren't members yet at 00:00. With the next midnight passed, 0 of 58
    were NULL.
  - Pass `as_of=end` for a reproducible snapshot as of the close of the window.
- **Timing:** the core SQL below, including the three correlated subqueries, ran in 6.3 s for site 24 over 3 days
  (12 joins). Re-measured on 2026-10-30 at site 24 over 14 days: 58 members in 3.3 s.
  - A full 31-day, 6-site call is about 1,000 joins, extrapolated to roughly a minute.
  - The procedure is costlier for long-tenured members (113 existing members took 72 s), but new members are cheap.
  - The docstring states this, and it recommends one site, or a window of 14 days or less, for interactive use.
- **Subquery hygiene:** the `LastCharge` subquery adds `AND (f_IsCompleted(s3.Status) = 1 OR
  f_IsIncomplete(s3.Status) = 1)`, so it counts only paid joins and renewals. `UsesSinceJoin` stays `COUNT(*)`,
  because no duplicate Status-0 rows were found (8.0).

```sql
SELECT j.Customer, j.JoinDate, j.JoinSite,
       m.FirstSaleSite, m.PlanType, cp.Name AS PlanName, m.PassStatus, f_ArmStatusName(m.PassStatus) AS PassStatusName,
       CAST(m.Amount AS NUMERIC(9,2)) AS BillingAmount, m.Expires, m.MemberSince, m.Members,
       (SELECT MAX(s2.LogDate) FROM V_SalePasses p2 JOIN V_Sale s2 ON s2.Site = p2.Site AND s2.ObjID = p2.SaleID
         WHERE p2.Customer = j.Customer AND p2.Status = 0) AS LastUsed,
       (SELECT COUNT(*) FROM V_SalePasses p2 JOIN V_Sale s2 ON s2.Site = p2.Site AND s2.ObjID = p2.SaleID
         WHERE p2.Customer = j.Customer AND p2.Status = 0 AND s2.LogDate >= j.JoinDate) AS UsesSinceJoin,
       (SELECT MAX(s3.LogDate) FROM V_SalePasses p3 JOIN V_Sale s3 ON s3.Site = p3.Site AND s3.ObjID = p3.SaleID
         WHERE p3.Customer = j.Customer AND p3.Status IN (1, 3)
           AND (f_IsCompleted(s3.Status) = 1 OR f_IsIncomplete(s3.Status) = 1)) AS LastCharge
       [, c.FirstName, c.LastName, c.Name, c.Company, c.Address1, c.Address2, c.City, c.State, c.ZipCode,
          c.MainPhone, c.AltPhone, c.Email, c.LastCustCode, c.SuppressMail, c.Created,
          (SELECT LIST(TRIM(cc.Code), ' ') FROM V_CustomerCode cc WHERE cc.Customer = c.ObjID) AS Codes]
FROM (SELECT p.Customer, MIN(s.LogDate) AS JoinDate, MIN(p.Site) AS JoinSite
        FROM V_SalePasses p
        JOIN V_Sale s      ON s.Site = p.Site AND s.ObjID = p.SaleID
        JOIN V_PlanType pt ON pt.ObjID = p.PlanType
       WHERE p.Status = 1 AND pt.Typ = 110
         AND (f_IsCompleted(s.Status) = 1 OR f_IsIncomplete(s.Status) = 1)
         AND s.Site IN (?, ...) AND s.LogDate >= ? AND s.LogDate < ?
       GROUP BY p.Customer) j
[JOIN V_Customer c ON c.ObjID = j.Customer]
LEFT JOIN Arm_SalePass_Member(j.Customer, ?) m ON 1 = 1
LEFT JOIN V_PlanType cp ON cp.ObjID = m.PlanType
ORDER BY j.JoinDate, j.Customer
```

- **`JoinSite`:** `MIN(p.Site)` is a tiebreak for the rare join at two sites in the window.
- **Site names:** looked up in Python from `sites.list(all ids)`, a cached lookup, instead of joining `V_Site` and
  `V_System` twice.
- **Recharge site** (the export's "latest charge site"): needs a `FIRST 1 ... ORDER BY` correlated subquery. It is
  **optional** (`include_recharge_site=False` default) because of the cost; measure it during implementation.
- **Vehicle fields** (Make, Model, Color, VIN) are unused at ICON (verified NULL), so they are not selected. Plates
  come from `V_CustomerCode.Code` and are included only with `include_contact=True`.
- **Excluded:** `V_SalePayments.MaskedPan` and `Expiration` (cardholder data). Birthdate is also dropped, because
  minimizing data matters more than mirroring the old export.

```python
class MemberContact(SiteWatchModel):
    first_name: str | None
    last_name: str | None
    full_name: str | None
    company: str | None
    address1: str | None
    address2: str | None
    city: str | None
    state: str | None
    zip: str | None
    main_phone: str | None
    alt_phone: str | None
    email: str | None
    last_code: str | None        # last plate/tag used
    codes: list[str] = []        # all plates/tags
    suppress_mail: bool | None
    customer_created_at: datetime | None

class NewMember(SiteWatchModel):
    customer_id: int
    join_date: date
    join_site_id: int
    join_site_code: str
    first_sale_site_id: int | None
    plan_type_id: int | None        # current plan (as of)
    plan_name: str | None
    pass_status: int | None
    pass_status_name: str | None    # Joined, Renewed, Terminated, ...
    billing_amount: float | None    # Arm_SalePass_Member.Amount
    next_bill_date: date | None     # Expires
    member_since: date | None
    family_members: int | None
    last_used_date: date | None
    uses_since_join: int
    last_charge_date: date | None
    contact: MemberContact | None = None   # only when include_contact=True
```

### 8.4 `member_status(customer_id: int, as_of: date | None = None) -> MemberStatus | None`

- **What it does:** returns one member's current ARM state through the cheap `Arm_SalePass_Member` procedure. It
  returned exactly one row per member for 125 members tested, so `None` means no row.
- **`as_of`:** same end-of-day rule as 8.3, sent as `as_of 23:59:59`. NULL means "now".
- **Cost:** under a second for a new member, but it grows with history. 113 members with existing use took 72 s in
  total, so callers should not loop it over hundreds of members.
- **PII:** none returned.

```sql
SELECT m.Leader, m.Member, m.Site, m.SaleID, m.PlanType, pt.Name, m.PassStatus, f_ArmStatusName(m.PassStatus),
       f_StatusCode(m.SaleStatus), m.SalePaidTS, m.WashesSold, m.WashesUsed, m.WashesLeft, m.Expires,
       m.FirstSaleSite, m.MemberSince, m.Members, CAST(m.Amount AS NUMERIC(9,2)), CAST(m.Upsell AS NUMERIC(9,2))
FROM Arm_SalePass_Member(?, ?) m
LEFT JOIN V_PlanType pt ON pt.ObjID = m.PlanType
```

```python
class MemberStatus(SiteWatchModel):
    customer_id: int
    leader_id: int | None
    site_id: int | None
    sale_id: int | None
    plan_type_id: int | None
    plan_name: str | None
    pass_status: int | None
    pass_status_name: str | None
    sale_status_code: str | None
    sale_paid_at: datetime | None
    washes_sold: int | None
    washes_used: int | None
    washes_left: int | None
    expires: date | None
    first_sale_site_id: int | None
    member_since: date | None
    family_members: int | None
    amount: float | None
    upsell: float | None
```

### 8.5 `events(start, end, site, *, statuses: Iterable[int] | None = None) -> list[MembershipEvent]`

- **Range limit:** 7 days.
- **What it returns:** row-level ARM events (join, renew, used, decline, terminate and so on) for one site. This is
  the drill-down behind `activity()`. It returns IDs only, no PII.

```sql
SELECT sp.Site, sp.SaleID, sp.Seq, s.LogDate, s.LogDate + s.LogTime / 14400.0 AS EventAt,
       f_StatusCode(s.Status) AS SaleStatusCode, sp.Customer, sp.Leader, sp.PlanType, pt.Name AS PlanName,
       sp.Status, f_ArmStatusName(sp.Status) AS StatusName, sp.Washes, sp.Expires, sp.Members,
       CAST(sp.Amount AS NUMERIC(9,2)) AS Amount, CAST(sp.Upsell AS NUMERIC(9,2)) AS Upsell, sp.FirstSaleSite
FROM V_Sale s
JOIN V_SalePasses sp ON sp.Site = s.Site AND sp.SaleID = s.ObjID
JOIN V_PlanType pt ON pt.ObjID = sp.PlanType
WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ? [AND sp.Status IN (?, ...)]
ORDER BY s.LogDate, s.LogTime, sp.SaleID, sp.Seq
```

- **Sale statuses:** all are included. Declines, expiries and discontinues are `Notes` sales. Joins and uses are
  `C:PW` or `I:P-W`, recharges and terminations are `C:P`, and discontinuing events occur on all three. This was
  verified at site 24 over 2026-10-01..14. `sale_status_code` lets callers filter.
- **Duplicates:** the docs state that duplicate rows per sale are kept, and that SiteWatch reports use the highest
  `Seq`, with ties broken on Status.
- **Model:** `MembershipEvent` has the fields `site_id`, `sale_id`, `seq`, `log_date`, `event_at`,
  `sale_status_code`, `customer_id`, `leader_id`, `plan_type_id`, `plan_name`, `status`, `status_name`, `washes`,
  `expires`, `family_members`, `amount`, `upsell` and `first_sale_site_id`.

### 8.6 Left out on purpose (documented in the memberships guide)

- **`active_members(as_of)` through `Arm_Monthly_Current_Status`.** It computes every member at every site:
  - a `FIRST 5` took 38 s;
  - the grouped count per RechargeSite and Status **did not finish in 200 s** (probe on 2026-10-30);
  - its output also includes members who dropped within the 2 prior months, plus legacy sites.

  It is too heavy for a library method on the shared server. The guide shows the raw `query()` form, with a warning
  to run it off-hours, and suggests asking DRB for a scheduled ARM Summary export.
- **`summary(start, end)` through `Arm_Summary_For_Date_Range`.** A one-week run timed out after 175 s. It would give
  start and end member counts, which nothing else provides, so it is listed as future work, possibly as an opt-in
  `memberships.summary(..., i_understand_this_is_slow=True)` limited to 1 day, once it has been timed off-hours.
- **`CustClubPlan_Balances`:** it timed out on one week, and club plans are not ARM.

---

## 9. `client.labor`: LaborResource

### 9.0 Business rules (verified)

- **Timecard source:** `V_PunchInterval`, with one row per clocked interval.
  - `InTime` and `OutTime` are minutes after midnight of `PunchDate`, and `OutTime` can be 1440.
  - Clock-in is `PunchDate + InTime/1440`.
  - Hours are `(OutTime - InTime) / 60`, with no rounding (`RoundPunchTo = 0`).
  - Only PiType `R` and `O` occur at ICON. Other PiTypes (`A` adjustments, `N` non-work) are excluded by default,
    the same as the export.
- **`ActualIn` and `ActualOut`** hold tenths of a minute or one of these codes:

  | Code | Meaning |
  |---|---|
  | 30000, 30003, 30004 | Missing out (OPEN) |
  | 30001, 30002 | Edited in the Timecard Editor |
  | -30000 | Day-change continuation |
  | -30001, -30002 | OT split |
  | -1 to -28800 | Group punch |

  A value from 0 to 28800 that isn't the "same minute" as the recorded time means the punch was edited. Same minute
  means `x // 10 == m`, or `x % 10 == 0 and x // 10 - 1 == m`. That last case is the **rounding exception** (it cut
  EDITED from 169 to 111 at validation).
- **Pay weeks** run Saturday to Friday (`WorkWeekStartDay = 7`). SiteWatch applies 40-hour weekly OT **per site**.
- **Daylight saving:** `InTime` and `OutTime` are wall-clock minutes, so an interval spanning 02:00 on a DST
  change night is 1 hour off from elapsed time. It is 1 hour short in November (2026-11-01 is the next one) and 1
  hour long in March. `Labor_Statistics` uses the same arithmetic, so the library keeps SiteWatch's hours
  unchanged. It adds a `TimecardReport.warnings` entry naming each interval whose `PunchDate` is a US DST change
  date and whose span covers 02:00, so payroll can review it.
- **Site scope for cross-site checks:** OVERLAP and `weekly_hours_all_sites` only see the sites requested. Office
  99 and query server 100 had **no** punches in 2026-10-01..14 (verified), so the car-wash default loses nothing
  today. But 3 of the 43 employees punching at ICON sites in that window have a legacy (non-ICON) home site.
  Narrowing `sites` hides cross-site overlaps and OT, and the docstring says so.
- **Validation:** `Labor_Statistics(start, end, site, NULL, 1, NULL, NULL, NULL, NULL, NULL)` equals the export
  minutes by employee and PiType at all six sites (09-26 to 10-09). All-NULL order arguments error inside the
  procedure, so at least one Order argument must be non-NULL.
  - Re-checked 2026-10-30 at site 24 for 2026-10-03..09: `Labor_Statistics` gave R 13,079 and O 1,729 minutes.
    That equals `SUM(OutTime - InTime)` for `PunchDate >= 10-03 AND < 10-10`, and not for `< 10-09` (R 12,175,
    O 404). `ThruDate` is therefore **inclusive**.
- **Privacy:** `V_Employee` is selected by explicit column list only. Pay rates exist (`Rate` is $/minute and
  `WageRate` is $/h) but are **not** returned by `timecards` or `employees`. `labor_statistics` returns the
  procedure's `Dollars` only when `include_dollars=True`.

### 9.1 `employees(sites=None, *, active_only: bool = True, employee_ids: Iterable[int] | None = None) -> list[Employee]`

- **Filter:** home site only. It filters `e.HomeSite IN (...)`, or `e.ObjID IN (...)` when `employee_ids` is given
  (validated ints, `?` placeholders, at most 500 IDs). People who float between sites are found through
  `worked_employees` (9.5).
- **`active_only`:** `e.Enabled = 1 AND (e.TermDate IS NULL OR e.TermDate > ?)`, where the parameter is today's date
  computed in Python, not the server's `CAST('TODAY' AS DATE)`. `Enabled` is 1 or 2 (2 = disabled), never 0.
  Measured 2026-10-30, ICON plus office home sites:
  - Enabled 1 with no TermDate: 174;
  - Enabled 1 with a **past** TermDate: 137;
  - Enabled 2 with no TermDate: 120;
  - Enabled 2 with a past TermDate: 35.

  `Enabled` alone is therefore **not** enough: 137 enabled records are past their TermDate. Of the 61 employees who
  punched at ICON sites from 2026-08-15 to 10-16, 60 have no TermDate and 1 is enabled with a past TermDate (still
  punching). So this filter misses about 1 in 60 real workers. The docstring says so and points to
  `worked_employees` as the authoritative "who works here" list.

```sql
SELECT e.ObjID, e.Code, e.FirstName, e.LastName, e.Name, e.HomeSite, e.Category, c.Name AS CategoryName,
       e.HireDate, e.TermDate, e.Enabled
FROM V_Employee e
LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category      -- N:0..1, some categories missing
WHERE e.HomeSite IN (?, ...) [AND e.Enabled = 1 AND (e.TermDate IS NULL OR e.TermDate > ?)]
ORDER BY e.LastName, e.FirstName, e.ObjID
```

The query never selects `SSNUM`, `PWORD`, `SECURITYCODE`, `DRVLNUM`, `WEB*`, the address, phones, birthday, wage or
W4/I9. A unit test asserts that the SQL text contains none of the sensitive names.

```python
class Employee(SiteWatchModel):
    employee_id: int          # V_Employee.ObjID (internal key, joins to punches)
    employee_number: int | None  # V_Employee.Code (human / payroll number)
    first_name: str | None
    last_name: str | None
    full_name: str | None
    home_site_id: int | None  # may be a legacy OLD-* site
    category_id: int | None
    category: str | None      # Store Manager, Line Labor, Sales Labor, Admin, ...
    hire_date: date | None
    term_date: date | None
    enabled: bool
```

### 9.2 `timecards(start, end, sites=None, *, pi_types=("R", "O")) -> list[TimecardInterval]`

- **Range limit:** 31 days.
- **Port of:** `export_timecards.py`, the same SQL and logic, with sites and dates parameterized.

```sql
SELECT p.Site, s.Code AS SiteCode, s.SiteName,
       p.Employee AS EmployeeID, e.Code AS EmployeeNumber, e.FirstName, e.LastName, e.Name AS FullName,
       e.HomeSite, c.Name AS EmpCategory, p.Department AS LaborRoleID, r.Name AS LaborRole, p.PiType,
       p.PunchDate, p.InTime, p.OutTime, p.ActualIn, p.ActualOut, p.Multiplier
FROM V_PunchInterval p
JOIN V_Site s ON s.ID = p.Site
LEFT JOIN V_Employee e ON e.ObjID = p.Employee
LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category
LEFT JOIN V_EmpRole r ON r.ObjID = p.Department
WHERE p.Site IN (?, ...) AND p.PunchDate >= ? AND p.PunchDate < ? AND p.PiType IN (?, ...)
ORDER BY e.LastName, e.FirstName, p.Employee, p.PunchDate, p.InTime, p.Site
```

`Rate` is deliberately not selected.

Post-processing in `resources/_timecards.py` is pure, unit-tested functions copied in behaviour from
`export_timecards.py`:

- `same_minute(tenths, minute)` and `classify_actual(actual, minute) -> PunchSource` return one of `terminal`,
  `edited`, `missing_out`, `continuation`, `group`, `unknown` or `other`.
- `week_start(day) -> date` returns the Saturday.
- `build_intervals(rows) -> list[TimecardInterval]` computes the flags, `new_shift` and `gap_before_min`. The
  OVERLAP check depends on the ordering above (employee, date, InTime), so the SQL `ORDER BY` is part of the
  contract.
- The flags are:
  - **OPEN** when `out_source == missing_out`;
  - **EDITED** when either side is `edited`;
  - **CROSSES_MIDNIGHT** when `OutTime >= 1440` and `ActualOut == -30000`;
  - **SW_OVERTIME** when PiType is `O`;
  - **OVERLAP** when clock-in is earlier than the same employee's latest earlier clock-out at any site.

```python
class PunchSource(str, Enum):
    TERMINAL = "terminal"; EDITED = "edited"; MISSING_OUT = "missing_out"
    CONTINUATION = "continuation"; GROUP = "group"; UNKNOWN = "unknown"; OTHER = "other"

class TimecardFlag(str, Enum):
    OPEN = "OPEN"; EDITED = "EDITED"; CROSSES_MIDNIGHT = "CROSSES_MIDNIGHT"
    SW_OVERTIME = "SW_OVERTIME"; OVERLAP = "OVERLAP"

class TimecardInterval(SiteWatchModel):
    site_id: int
    site_code: str
    site_name: str
    employee_id: int
    employee_number: int | None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    home_site_id: int | None
    employee_category: str | None
    labor_role_id: int | None
    labor_role: str | None
    punch_date: date
    pay_week_start: date            # Saturday
    clock_in: datetime
    clock_out: datetime             # OutTime 1440 -> next day 00:00
    hours: float                    # (OutTime-InTime)/60, 4 dp
    pi_type: str                    # R / O
    multiplier: float | None        # 1.0 / 1.5
    in_source: PunchSource
    out_source: PunchSource
    new_shift: bool
    gap_before_min: int | None      # break/lunch gap since previous clock-out the same day
    flags: list[TimecardFlag]
    actual_in_raw: int | None
    actual_out_raw: int | None
```

### 9.3 `timecard_totals(start, end, sites=None) -> TimecardReport`

- **What it does:** calls `timecards()` once, then aggregates in Python, the same as `build_totals`.
- **No extra SQL.** `TimecardReport` carries both the intervals and the totals, so the caller doesn't query twice.
- **Range limit:** 31 days.

```python
class WeeklyHours(SiteWatchModel):
    pay_week_start: date
    hours: float                     # all requested sites combined (for cross-site OT review)

class TimecardTotal(SiteWatchModel):
    site_id: int
    site_code: str
    site_name: str
    employee_id: int
    employee_number: int | None
    first_name: str | None
    last_name: str | None
    home_site_id: int | None
    employee_category: str | None
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
    period_start: date
    period_end: date
    whole_pay_weeks: bool            # start is a Saturday and end is a Friday
    latest_punch_date: date | None
    intervals: list[TimecardInterval]
    totals: list[TimecardTotal]
    warnings: list[str]              # same conditions the script printed: data ends before `end`, OPEN, OVERLAP, partial weeks
```

The script's printed warnings become `TimecardReport.warnings` and are also logged with `logging.getLogger(__name__)`
at WARNING. The library never prints to stdout. No overtime pay is computed; the docs reference the open payroll
questions in the schema map.

### 9.4 `labor_statistics(start, end, site, *, group_by=("employee",), profit_center=None, labor_role=None, employee_category=None, employee_id=None, include_dollars=False) -> list[LaborStat]`

- **Range limit:** 31 days.
- **Wraps:** the `Labor_Statistics` procedure for one site. Its main use is cross-checking `timecards`.
- **`group_by`:** a non-empty ordered subset of `{"date", "employee", "role"}`. The positions become `DateOrder`,
  `EmployeeOrder` and `RoleOrder` with values 1 to 3, and NULL for anything left out. An empty `group_by` raises
  `ValidationError`, because all-NULL order arguments crash the procedure.

```sql
SELECT ls.PunchDate, ls.Employee, e.Code AS EmployeeNumber, e.FirstName, e.LastName,
       ls.Role, r.Name AS RoleName, ls.PIType, ls.Minutes, CAST(ls.Dollars AS NUMERIC(12,2)) AS Dollars
FROM Labor_Statistics(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ls
LEFT JOIN V_Employee e ON e.ObjID = ls.Employee
LEFT JOIN V_EmpRole r ON r.ObjID = ls.Role
```

The parameters are `FromDate`, `ThruDate` (**inclusive** end date, as the procedure expects), `Site`, `DateOrder`,
`EmployeeOrder`, `RoleOrder`, `ProfitCenter`, `LaborRole`, `EmployeeCategory` and `EmployeeID`.

```python
class LaborStat(SiteWatchModel):
    site_id: int
    punch_date: date | None          # set when grouped by date
    employee_id: int | None
    employee_number: int | None
    first_name: str | None
    last_name: str | None
    role_id: int | None
    role: str | None
    pi_type: str | None
    minutes: float
    hours: float                     # minutes / 60, 2 dp
    dollars: float | None            # None unless include_dollars=True
```

### 9.5 `worked_employees(start, end, sites=None) -> list[Employee]` (added in review)

- **Range limit:** 93 days.
- **What it returns:** every employee with at least one punch at `sites` in the window, whatever their home site,
  Enabled flag or TermDate. It's the answer to "who works at these sites", and it covers floaters from legacy home
  sites (3 of 43 in 2026-10-01..14).
- **Statements:** one. A derived table plus a `JOIN`, never `IN (SELECT ...)`. This exact SQL returned 43 employees
  for 6 ICON sites over 2026-10-01..14 in 0.2 s. The same form ran in 0.2 s for 8 sites over 2 months.

```sql
SELECT e.ObjID, e.Code, e.FirstName, e.LastName, e.Name, e.HomeSite, e.Category, c.Name AS CategoryName,
       e.HireDate, e.TermDate, e.Enabled
FROM (SELECT p.Employee FROM V_PunchInterval p
       WHERE p.Site IN (?, ...) AND p.PunchDate >= ? AND p.PunchDate < ?
       GROUP BY p.Employee) w
JOIN V_Employee e ON e.ObjID = w.Employee
LEFT JOIN V_EmpCategory c ON c.ObjID = e.Category
ORDER BY e.LastName, e.FirstName, e.ObjID
```

### 9.6 `daily_hours(start, end, sites=None) -> list[DailyLaborHours]` (added in review)

- **Range limit:** 93 days.
- **What it returns:** clocked hours per site per day, split by PiType. It's the denominator for cars per labor
  hour (CPLH), the standard car-wash labor KPI, and it needs no employee columns at all.

```sql
SELECT p.Site, p.PunchDate, COUNT(DISTINCT p.Employee) AS Employees,
       SUM(CASE WHEN p.PiType = 'R' THEN p.OutTime - p.InTime ELSE 0 END) AS RegularMinutes,
       SUM(CASE WHEN p.PiType = 'O' THEN p.OutTime - p.InTime ELSE 0 END) AS OvertimeMinutes,
       SUM(CASE WHEN p.ActualOut IN (30000, 30003, 30004) THEN 1 ELSE 0 END) AS OpenPunches
FROM V_PunchInterval p
WHERE p.Site IN (?, ...) AND p.PunchDate >= ? AND p.PunchDate < ? AND p.PiType IN ('R', 'O')
GROUP BY p.Site, p.PunchDate
```

- **Measured** for 6 ICON sites over 2026-10-01..14: 84 site-days, 167,363 minutes and 25 OPEN punches, in 0.3 s.
- The minutes equal the per-PiType sum, and the per-PiType sum equals `Labor_Statistics` (9.0).
- Employees are counted in one `GROUP BY` without PiType **on purpose**. Grouping by PiType and adding the groups
  up double-counts anyone with both R and O on a day: 370 instead of 345 for the same window.
- The `'R', 'O'` literals are library constants, not caller input.

```python
class DailyLaborHours(SiteWatchModel):
    site_id: int
    site_code: str
    punch_date: date
    employees: int
    regular_hours: float      # PiType R
    overtime_hours: float     # PiType O
    total_hours: float
    open_punches: int         # intervals with ActualOut 30000/30003/30004 (placeholder hours)
```

### 9.7 `productivity(start, end, sites=None) -> list[DailyProductivity]` (added in review)

- **Range limit:** 31 days.
- **What it does:** a Python merge of `sales.daily_summary` and `labor.daily_hours` on `(site, date)`, with no new
  SQL.
- **Fields:** `site_id`, `site_code`, `log_date`, `car_count`, `labor_hours`, `cars_per_labor_hour` (None when
  hours are 0) and `revenue_per_labor_hour`.
- **Docstring caveat:** OPEN punches carry placeholder hours, so CPLH for a day with OPEN punches is provisional.
  The method carries `open_punches` through from `daily_hours`. There were 25 OPEN punches across ICON in
  2026-10-01..14, so this is common.

---

## 10. `client.catalog`: CatalogResource

### 10.1 `items(*, include_disabled: bool = False, report_category: str | None = None) -> list[Item]`

```sql
SELECT i.ObjID, i.Name, i.Number, i.ItemType, t.Name AS ItemTypeName, t.BaseType,
       i.ReportCategory, rc.Name AS Category, rc.Branch, i.ProfitCenter, i.IsPackage, i.PlanType,
       i.ActiveAt, i.Enabled
FROM V_Item i
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE 1 = 1 [AND i.Enabled = 1] [AND rc.Branch STARTING WITH ?]
ORDER BY rc.Branch, i.Name
```

About 4,848 rows, global, with no Site column. `ActiveAt` is returned raw:

- a value above 0 is a site;
- a value below 0 is a negated site group;
- -2147483647 means all sites;
- 0 means unassigned.

The price (`STDPRICE` / `V_ItemValue1`) is out of v1, because its effective-date semantics are unverified.

```python
class Item(SiteWatchModel):
    item_id: int
    name: str
    number: int | None
    item_type_id: int
    item_type: str
    base_type: int
    base_type_name: str
    report_category_id: int
    report_category: str
    category_branch: str
    profit_center: int | None
    is_package: bool
    plan_type_id: int | None
    active_at: int | None
    enabled: bool
```

### 10.2 `report_categories() -> list[ReportCategory]`

```sql
SELECT rc.ObjID, rc.Name, rc.Branch FROM V_ItemRptCategory rc ORDER BY rc.Branch
```

There are 52 rows, verified (see section 12). The `parent_branch` and `depth` fields are derived in Python from the
3-digit branch path.

```python
class ReportCategory(SiteWatchModel):
    category_id: int
    name: str
    branch: str
    depth: int
    parent_branch: str | None
```

### 10.3 `terminals(sites=None, *, include_disabled=False) -> list[Terminal]`

```sql
SELECT t.ObjID, t.Site, t.Name, t.Number, t.Enabled FROM V_Terminal t WHERE t.Site IN (?, ...) [AND t.Enabled = 1]
```

`Terminal` has the fields `terminal_id`, `site_id`, `name`, `number` and `enabled`. Join on ObjID, because `Number`
is not unique.

Membership plans live in `memberships.plans()` and are not duplicated here.

---

## 11. Future work (not in v1)

- Retail vs member car count (section 7.0), once it reconciles against a SiteWatch Daily Summary report.
- Start and end member counts (`Arm_Summary_For_Date_Range`), after an off-hours timing, or from a DRB export.
- House-account charges (`V_SaleCharges` AcctType H/P → `V_Account`), as a 1-day-limited method.
- Average service and wait time (`V_SaleFacts` ServTime, WaitTime and ServiceTime in PC 1; units unverified).
- First-visit counts (the `FirstVisit` fact), and visit frequency (SWVST*).
- Every query that has been checked against a SiteWatch report gets logged as "validated" in the docs, following the
  DRB data-validation protocol.

---

## 12. Live evidence gathered for this spec (2026-10-30, read-only, one query at a time)

| Probe | Result |
|---|---|
| ICON sites via `V_Site` ⟕ `V_System` | 8 rows (5, 13, 18, 20, 24, 25, 99, 100), all Enabled = 1, with addresses |
| `V_SaleFacts` facts at site 24, 2026-10-14 | PC 0 and PC 1 both have `TotalCarCount` = `CountedCarCount` = 172; PC 1 `SaleCount` 173 vs PC 0 225; fact ObjIDs 54200/54181 |
| Status mix at site 24, 2026-10-14 | C:P 57 ($1,598.09), C:PW 164 ($514.00), I:P-W 8, Notes 217 ($6,570.72, not completed) |
| Car count vs lines | TotalCarCount sales = sales with a Basic Washes (`001001001`) item = 164 C:PW + 8 I:P-W; 0 for C:P and Notes |
| Car count, site 5, 10-12 and 10-13 | TotalCarCount = CountedCarCount = 184 / 191, PC 0 = PC 1 |
| Line mix by category and base type, site 24, 10-14 | Basic Washes 164 lines $3,024; ARM Recharged 52 lines; ARM Sold 8; tenders (BT 180) sum to -2,112.09 = -(sales total); XPT deposit BT 170 -76.00 |
| Report categories | 52 rows, branch tree confirmed |
| `V_ARM_Monthly_Activity`, site 24, 10-13 and 10-14 | ~385 rows/day = active leaders with activity; SaleCount 2/8, RechargeCount 60/52, matching line counts |
| `Arm_Monthly_Current_Status('2026-10-14')` | FIRST 5 in 38 s; grouped count **> 200 s (aborted)**; output includes non-ICON RechargeSites |
| new_members core SQL with `Arm_SalePass_Member` + uses subquery, site 24, 10-12 to 10-14 | 12 rows in 6.3 s |
| Sale headers with `f_SaleScr`, `V_SaleStats`, terminal and code joins, site 24, 1 day | ~6 s; `PaidAt` = `TStamp`; recharges on terminal "Automatic Recharge" carry ActualSaleID |
| *Review probes (2026-10-30, same rules):* | |
| Punches and sales at sites 99 and 100, 10-01..14 | 0 and 0 |
| Base-type mix, site 24, 10-14, completed | 100: 0; 105: 0; 120: 3,024; 140: 1,837.02; 150: -2,637; **160: -35.93**; 170: -76; 180: -2,112.09; 200: 0. The cast returned `1837.0200000000011` (float noise survives `NUMERIC(12,2)`) |
| Base types 160 and 170 items, site 24, 10-01..14 | 160 = "ARM Refund" plus the per-plan "... Rfnd" items (-529.29); 170 = XPT Acceptor Deposit (-1,325.00) |
| Report-category duplicate names | 0 |
| Freshness probes, site 24 | `FIRST 1 ... ORDER BY LogDate DESC` on V_SaleStats **19.7 s**; `MAX(LogDate)` on V_Sale with a 1-year floor 0.9 s; punches 0.4 s and 0.2 s |
| Status mix, site 24, 10-01..14 | C:P 882; C:PW 2,287; **I:P-W 66 ($109.98, tendered)**; Notes 2,633 ($81,056.41); **PendAppr 1** ($0) |
| `V_SalePasses` by sale status, site 24, 10-01..14 | Joins: C:PW 58 + I:P-W 2; Used: C:PW 1,627 + I:P-W 59; rows = distinct SaleIDs in every group |
| `V_ARM_Monthly_Activity`, site 24, 10-01..14 | SaleCount 60, WashUsed 1,686, Declined 2,030, Expired 42, Discontinued 52; 6 sites in 1.1 s |
| `Arm_SalePass_Member` rows per member | Always 1 (12 new members; 113 members who washed, which took 72 s) |
| new_members core SQL, site 24, 10-01..14 | 58 members in 3.3 s; as_of `'2026-10-14'` gives 7 NULL states, as_of next midnight gives 0; revised paid SQL with `23:59:59` gives 60 members, 0 NULL, 0.5 s |
| Employee Enabled × TermDate (ICON and office home sites) | 1/none 174; 1/past 137; 2/none 120; 2/past 35. Punchers 08-15..10-16: 60 none, 1 enabled with a past TermDate |
| Punchers at ICON, 10-01..14 | 43, of whom 3 have a legacy home site and 3 an office home site |
| `Labor_Statistics` ThruDate | Inclusive: 10-03..09 equals `PunchDate < 10-10` (R 13,079 and O 1,729) |
| Hourly car count, site 24, 10-14 | Sums to 172 in 0.2 s |
| Car count by status, site 5, 10-01..14 | C:PW 2,609 + I:P-W 42; no other status carries the fact |
| ActualSaleID targets of completed sales, site 24, 10-01..14 | 835 point before the window, 1 inside it (a recharge chained to a join) |
| daily_hours single-pass SQL, 6 sites, 10-01..14 | 84 site-days, 167,363 minutes, 345 employee-days (370 if summed per PiType), 25 OPEN, 0.3 s |

---

## 13. Testing strategy

- **Unit tests** (no database): `conftest.mock_client` patches `SiteWatchConfig.load_config` and sets
  `client._conn = MagicMock()`. Each resource test checks four things:
  1. the SQL sent contains the right placeholders and **no** user value interpolated: the params tuple carries them;
  2. the params are in the right order, including `end + 1 day` for the half-open filter;
  3. DataFrame-to-model mapping, including None/NaN handling, trimming and money rounding;
  4. the range-limit and site-validation errors.
- **Safety tests:**
  - `assert_read_only_sql` accepts SELECT/WITH, selectable documented procedures and comments. It rejects DML, DDL,
    `EXECUTE`, multiple statements, every blocked procedure, `*_UPDATE_*`, the SSN UDFs, sensitive employee columns,
    and `SELECT *` from `V_Employee` / `V_Site`.
  - Every resource's SQL constant contains no sensitive column names.
- **Timecard logic tests:** table-driven cases for `same_minute` (including the rounding exception), `classify_actual`
  for every code, `week_start`, and `build_intervals` flags (OPEN, EDITED, CROSSES_MIDNIGHT, SW_OVERTIME, OVERLAP,
  gap, new_shift), using synthetic rows with made-up names and IDs.
- **Integration tests** (`-m integration`, skipped without `SITEWATCH_*` in the environment):
  - one site with 1-3 day windows inside 2026-10-01..14;
  - the tie-out in 7.1;
  - car count = Basic Washes sales + I:P-W;
  - `timecards` minutes equal `labor_statistics` minutes per employee and PiType for one site and one pay week
    (2026-10-03..09). Site 24 totals are R 13,079 and O 1,729 minutes;
  - `activity_totals(...).joins == len(new_members(...))` for site 24 over 2026-10-01..14 (60, using the paid rule);
  - `new_members(..., as_of=end)` has no member with `pass_status is None` (the end-of-day `as_of` rule);
  - `daily_summary` for site 24 on 2026-10-14 has `paidouts == -35.93` and `pending_wash_sales == 8`;
  - `sum(daily_hours minutes) == labor_statistics minutes` for one site and one pay week.
- **Unit tests added in review:**
  - `as_of` is sent as `23:59:59` of the given date;
  - `daily_summary` maps the IsCompleted split rows to the completed and pending fields;
  - money rounding is applied to noisy floats such as `2112.090000000001`;
  - the DST warning fires for a synthetic interval on 2026-11-01 spanning 02:00, and not on 2026-10-31;
  - `daily_hours` counts an employee with both R and O once;
  - `assert_read_only_sql` rejects the `V_Site` secret columns and `MASKEDPAN`.

  They assert aggregates only and never print or snapshot names.
- **Fixtures:** no real customer or employee data. Use made-up values such as "Pat Example" or employee number 99999.

---

## 14. Docs

The docs mirror Rinsed's `mkdocs.yml`: material theme, mkdocstrings with Google docstrings, and `paths: [src]`. The
guides are:

- getting-started: install, `.env`, the Firebird 2.5 client DLL, first query, `data_freshness`;
- sites;
- sales: definitions of completed, car count, revenue and tender; recipes;
- catalog;
- memberships: ARM statuses, what is and isn't available, and why;
- labor: punch codes, flags, pay weeks, DST caveat, active vs worked employees, CPLH (`productivity`), open
  payroll questions;
- raw-queries: `query()` rules, Firebird dialect-1 cheatsheet, heavy-procedure recipes;
- safety: read-only design, blocked procedures, privacy;
- error-handling.

`CLAUDE.md` copies Rinsed's "before committing" checklist (changelog, version bump in two places, docs redeploy). It
also adds the safety and privacy rules and the "never call" procedure list.

---

## 15. Review notes (adversarial review, 2026-10-30)

The review was made before any code was written. Every live check was read-only, ran one statement at a time, and
was bounded to one site (24 or 5) or the six ICON sites over 2026-10-01..14. Each one returned aggregates only; no
customer or employee values were printed or recorded. The raw figures are in the "Review probes" rows of section 12.

### Changes made

1. **`data_freshness` used a slow probe (performance, 6.3).** `SELECT FIRST 1 ... ORDER BY LogDate DESC` on
   `V_SaleStats` took 19.7 s at one site, and the method would run it six times. It was replaced with
   `MAX(LogDate)` on `V_Sale` plus a 400-day floor computed in Python (0.9 s), and the punch probe got the same
   form. The "time both later" note was removed.
2. **Paid-but-pending sales were inconsistent across methods (business logic, 7.0, 7.1).** `car_count` (the
   `TotalCarCount` fact) and the ARM roll-up count I:P-W sales, but `sales_total` and `new_members` excluded them.
   I:P-W sales are paid: 66 sales and $109.98 at site 24 in two weeks, with tenders balancing. A "paid" rule was
   defined. `daily_summary` (a) now splits completed from pending and adds `pending_wash_sales` and
   `pending_wash_total`. `sales_total` keeps the DRB completed-only meaning.
3. **Paid-outs were hidden inside revenue (business logic, 7.0, 7.1).** Base type 160 lines (ARM refunds:
   -35.93 on the tie-out day, -529.29 over two weeks) are part of `V_Sale.Total` but were not broken out. They were
   added to statement (c) and to `DailySales.paidouts` and `SalesTotals.paidouts`, and the tie-out table was
   extended. Revenue stays net of refunds, and the docs say so.
4. **New status `PendAppr` (7.0, 7.5).** It was observed live and is excluded by both UDFs. It is now documented,
   and `Sale.status_code` lists it.
5. **`new_members` join rule (business logic, 8.3).** Completed-only gave 58 joins against the roll-up's 60. The
   rule was switched to paid sales, so `new_members` reconciles with `activity()`. `LastCharge` was also limited to
   paid sales. The revised SQL was re-run: 60 members.
6. **`as_of=end` snapshot bug (business logic, 8.3, 8.4).** Passing a `date` sends midnight, so members who joined
   on the last day had no state: 7 of 8 on 2026-10-14. `as_of` is now sent as `23:59:59` of that day. Re-run: 0
   NULL states of 60.
7. **`include_recharge_site` was missing from the signature (8.3).** The text described the flag but the
   signature didn't have it. It was added.
8. **`Arm_SalePass_Member` cardinality and cost (8.3, 8.4).** It was verified to return 1 row per member (125
   members tested). Measured timings were added: 3.3 s for 58 new members, 72 s for 113 established members, and
   an extrapolated ~1 minute for a 31-day, 6-site call. Docstring guidance was added.
9. **ARM roll-up reconciliation (8.0).** It now carries two-week evidence that every roll-up counter equals the
   matching `V_SalePasses` count by sale status, and that Seq duplicates don't inflate per-status counts.
10. **Employee active filter (business logic, 9.1).** The spec was unsure whether `Enabled` alone suffices. It
    doesn't: 137 enabled records are past their TermDate. The combined filter also misses 1 of 61 real punchers.
    The filter was kept, with today now passed as a Python parameter instead of server `TODAY`. The confusing
    `by=` parameter was replaced with the `employee_ids` filter the text already described.
11. **Floaters were missed by home-site lookup (missing method, 9.5).** 3 of the 43 employees punching at ICON have
    legacy home sites. `labor.worked_employees(start, end, sites)` was added: a derived table plus JOIN, 0.2 s.
12. **Labor productivity was missing (missing methods, 9.6, 9.7).** The spec had no daily labor hours and no
    cars-per-labor-hour, the most common car-wash labor KPI. `labor.daily_hours` was added (verified SQL, 0.3 s).
    It counts employees without the PiType split, which would over-count 370 against 345. `labor.productivity`
    was added as a Python merge with `sales.daily_summary`, carrying OPEN-punch counts (25 in the window).
13. **Timezone and DST (5, 9.0).** Timestamps are now stated to be wall-clock local time with no offset. A
    `TimecardReport` warning was added for intervals spanning 02:00 on US DST change dates. The next one is
    2026-11-01, two days from now. Hours stay as SiteWatch computes them, the same as `Labor_Statistics`.
14. **Cross-site scope (9.0).** OVERLAP and weekly hours are limited to the requested sites. This is now
    documented, with evidence that sites 99 and 100 currently have no punches (the default loses nothing today).
15. **`Labor_Statistics` ThruDate (9.0).** It was verified inclusive: R 13,079 and O 1,729 minutes match
    `PunchDate < end + 1`, and don't match `< end`.
16. **Money casting (5).** `CAST(... AS NUMERIC(12,2))` was shown not to remove float noise in dialect 1
    (`2112.090000000001`). Python rounding is now stated as mandatory, a unit test was added for it, and the
    `Decimal` handling for `NUMERIC(9,2)` was noted.
17. **query() privacy guard (4).** It now rejects the `V_Site` secret columns by name (`PASS`, `EMAILPASS`,
    `MPSPASSWORD`, `SECRETACCESSKEY`, `ACCESSKEYID`, `FACEBOOKAPPSECRET`), because an explicit column list bypassed
    the `SELECT *` check. It also rejects `MASKEDPAN` and `*` on `V_SalePayments` and `V_SaleCharges`, in line with
    the no-card-data non-goal. Truncation by `max_rows` now logs a warning.
18. **Rinsed conventions (2).** All SQL now sits in `_query_builder.py`, as Rinsed does it. Resource modules must
    use `from __future__ import annotations`, because `def list` inside the class otherwise breaks the later
    `list[...]` annotations at import time. Rinsed's `_sites.py` does this.
19. **Small fixes.**
    - The `normalize_sites` wording contradicted itself on duplicates.
    - `tenders(by_item=True)` now groups by `i.ObjID` as well as the name.
    - The category lookup strips input, and an ambiguous match raises (names were verified unique).
    - `status_mix` documents that E:-P never appears.
    - `hourly_summary` now spells out its car-count SQL, verified to sum to the daily 172.
    - The `events()` status-by-sale-type statement was corrected (joins and uses also occur on I:P-W).
20. **Tests (13).** Integration tie-outs were added for the paid rule, `as_of`, paid-outs and pending, and
    `daily_hours` vs `Labor_Statistics`. Unit tests were added for `as_of` serialization, rounding, DST, R+O
    employee counting and the new guard columns.

### Checked and left unchanged (claims held up)

- Car count is the `TotalCarCount` fact in PC 1. At site 5 over two weeks it sits only on C:PW (2,609) and I:P-W
  (42) sales, and the fact names are unique in `V_Fact`.
- ActualSaleID chains do not double count completed sales inside a window: 835 of the 836 completed sales with an
  ActualSaleID point to a sale before it.
- Mixed name and ordinal `GROUP BY` works in Firebird 2.5 / dialect 1.
- `V_ARM_Monthly_Activity` column names match `catalog.json`. A 14-day, 6-site call takes 1.1 s, so the 93-day
  limit is reasonable.
- No sensitive `V_Employee` column is selected anywhere in the spec. `Rate` and `WageRate` stay out.
- Every history method takes a mandatory, capped date range plus a Site filter, and every user value is a `?`
  parameter.

### Open items for the owner (not changed, no evidence either way)

- Whether finance wants `sales_total` and revenue to include I:P-W, as car count does. The spec keeps DRB's
  completed-only rule and exposes the pending figures separately.
- Whether ARM refunds (paid-outs) should be reported against revenue (net, as now) or as a separate refund line in
  WashU's reporting.
- The retail vs member car-count split is still future work (section 11).
