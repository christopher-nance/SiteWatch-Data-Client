# Changelog

## 0.1.0 — Unreleased

### Added
- Package scaffold: `SiteWatchClient` with `connect()` / `close()` / context manager, `_execute()` and the
  `query()` escape hatch returning a pandas DataFrame.
- `SiteWatchConfig` dataclass and `load_config()` from `SITEWATCH_*` environment variables (password excluded from
  `repr`).
- `SiteWatchConnection`: `fdb` connection with SQL dialect 1 and charset `NONE`; every statement runs in its own
  read-only, read-committed transaction that is always rolled back. Lower-case column names, right-stripped CHAR
  padding, `bytes` decoded as Windows-1252, `date` parameters sent as midnight timestamps, `max_rows` cap.
- Read-only guard for `query()`: `SELECT`/`WITH` only, one statement, no DML/DDL, no data-changing procedures
  (DRB "never call" list plus `*_UPDATE_*` / `*_SET_*`), no SSN UDFs, no employee secrets, `V_Site` credentials or
  card data, and no `SELECT *` on `V_Employee`, `V_Site`, `V_SalePayments` or `V_SaleCharges`.
- `normalize_date`, `normalize_date_range` (inclusive input, half-open output, per-method day caps),
  `normalize_sites` (defaults to the ICON car-wash sites 5, 13, 18, 20, 24, 25), `placeholders()` and
  `build_in_clause()`.
- Exceptions: `SiteWatchError`, `ConfigurationError`, `ConnectionError`, `QueryError`, `ValidationError`,
  `UnsafeQueryError`.
- `SiteWatchModel` Pydantic base model.
- Unit tests (mocked connection) and opt-in live tests (`SITEWATCH_LIVE=1`).
- `client.sites` (`SitesResource`): `list()`, `get()`, `data_freshness()` returning `Site` and `SiteFreshness`.
- `client.sales` (`SalesResource`): `daily_summary()`, `totals()`, `hourly_summary()`, `item_sales()`,
  `tenders()`, `list()`, `status_mix()` returning `DailySales`, `SalesTotals`, `HourlySales`, `ItemSales`,
  `TenderTotal`, `Sale`/`SaleLine` and `StatusCount`. Car count is SiteWatch's `TotalCarCount` fact; paid but
  wash-pending (`I:P-W`) sales are reported separately.
- `client.catalog` (`CatalogResource`): `items()`, `report_categories()`, `terminals()` returning `Item`,
  `ReportCategory` and `Terminal`.
- `client.memberships` (`MembershipsResource`): `plans()`, `activity()`, `activity_totals()`, `new_members()`
  (contact details opt-in), `member_status()`, `events()` returning `MembershipPlan`, `MembershipActivity`,
  `MembershipActivityTotals`, `NewMember`/`MemberContact`, `MemberStatus` and `MembershipEvent`.
- `client.labor` (`LaborResource`): `employees()`, `worked_employees()`, `timecards()`, `timecard_totals()`,
  `labor_statistics()`, `daily_hours()` and `productivity()` (cars and revenue per labor hour), returning
  `Employee`, `TimecardInterval`, `TimecardReport`/`TimecardTotal`/`WeeklyHours`, `LaborStat`,
  `DailyLaborHours` and `DailyProductivity`. Timecard logic is a port of the validated payroll export (punch
  classification, OPEN/EDITED/OVERLAP/SW_OVERTIME/CROSSES_MIDNIGHT flags, Saturday-Friday pay weeks).
- Shared `SiteMetric` model; every resource and model exported from the package root.
- README, MkDocs site with a guide per resource and a mkdocstrings API reference.

### Changed (review fixes)
- **`query()` guard hardened.** Stored procedures and UDFs are now an allowlist: only the 11 DRB-documented procedures
  and 30 documented `f_*` UDFs may be called; every other routine the database declares (for example `F_IBPASSWORD`,
  `F_QPUSHQUEUE`, `F_SPPUT`, `ARM_SALEPASS_LEADER_CHANGE`) and any other `f_*(...)` call is rejected. `GEN_ID` and
  `NEXT VALUE FOR` are rejected (generator increments survive a rollback). The select-star check is now token-based,
  so `SELECT FIRST(1)*`, `DISTINCT*`, `ALL*`, `SKIP(n)*` and a newline/tab before `*` are caught. Payment tokens
  (`TOKEN`, `CVVR`, `AUTHCODE`) and other secrets are blocked, and `V_SaleArmPlan` (plus views REPORTS cannot read
  today) joined the no-star list. Words in double quotes are checked too, so a dialect-3 connection cannot bypass the
  guard with quoted identifiers.
- `query()` logs a warning when a history view is read without a `WHERE` clause or a procedure known to run for
  minutes is called; docs state that `max_rows` does not limit server work.
- Connections reuse one read-only `fdb` Transaction (begun and rolled back per statement, cursors closed), instead of
  creating a new Transaction per query that `fdb` kept until the connection closed.
- `SiteWatchClient.connect()` is a no-op when already connected (a second call no longer leaks a server attachment).
- `SiteWatchClient(port=...)` now always overrides `SITEWATCH_PORT`, including `port=3050`.
- "Today" (`labor.employees(active_only=True)`, `sites.data_freshness`) is the US Central business date, not the
  host's local date.
- **Labor open shifts.** New `TimecardFlag.OPEN_CHAIN` marks every piece of a shift with no real clock-out, including
  the `-24:00` piece before an open next-day continuation and overtime-split pieces. `TimecardTotal.open_hours` now
  counts the whole placeholder shift (previously only the open-coded fragment). `timecards()` reads one look-ahead
  day to see continuations past `end`, and the OPEN warning reports the placeholder hours.
- **`labor.daily_hours()` / `labor.productivity()`** exclude office accounts (home site 99) by default
  (`exclude_home_sites=(99,)`, `None` counts everyone) and report `excluded_hours`; `open_punches` now counts
  open-chain intervals, with new `open_hours` and `provisional` fields. `daily_hours` aggregates interval rows in
  Python.
- **`labor.productivity().revenue_per_labor_hour`** divides the new `wash_revenue` (revenue minus the `Automatic
  Recharge` terminal's completed sales, new `recharge_revenue`), so ARM billing days no longer swing it by 10x.
- **`memberships.new_members()`** is documented as paid joins *including rejoins*; new `prior_joins` / `is_rejoin`
  fields and a `first_join_only` flag. `join_site_id` is now the site of the earliest in-window join (it was the
  lowest site ID).
- `ARM_STATUS` names now match `f_ArmStatusName` exactly (`CC Expired`, `CC Declined`, `CC Information Required`,
  `CC Approved`).
- Docs: range-limit wording ("may span at most N days"), the `Arm_Monthly_Current_Status` example is guarded and
  carries the measured run time, export examples write to `exports/`, the design spec uses a placeholder database
  path, and `ruff format` excludes Markdown.
- Tests: join-key snapshot tests for every SQL builder, guard tests for each bypass, and live checks for the employee
  join, office exclusion, open chains, recharge revenue, rejoins and transaction reuse.
