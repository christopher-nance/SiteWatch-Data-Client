# The Client

`SiteWatchClient` is the entry point to the package. It holds the connection settings, opens a
Firebird connection to the SiteWatch query server and gives you the typed resources
(`client.sites`, `client.sales`, `client.memberships`, `client.labor`, `client.catalog`), plus
`client.query()`, a read-only escape hatch for ad-hoc SQL.

The database is the live DRB SiteWatch central database (Firebird 2.5, SQL dialect 1). About 60
sites share it, so the client is read-only by design.

## Configuration

### Environment Variables

Create a `.env` file in your project (copy `.env.example`):

```
SITEWATCH_HOST=your-query-server-host-or-ip
SITEWATCH_PORT=3050
SITEWATCH_DATABASE=E:\path\to\SiteWatch.FDB
SITEWATCH_USER=REPORTS
SITEWATCH_PASSWORD=change-me
SITEWATCH_FB_CLIENT=C:\path\to\fbclient25\bin\fbclient.dll
```

| Variable | Required | Meaning |
|----------|----------|---------|
| `SITEWATCH_HOST` | yes | Query server host name or IP. Any reachable host works; nothing is hard-coded. |
| `SITEWATCH_DATABASE` | yes | Server-side path of the `.FDB` file. |
| `SITEWATCH_USER` | yes | Firebird user, normally the read-only `REPORTS` user. |
| `SITEWATCH_PASSWORD` | yes | Firebird password. |
| `SITEWATCH_PORT` | no | Defaults to `3050`. Must be an integer. |
| `SITEWATCH_FB_CLIENT` | no | Absolute path to the Firebird **2.5** `fbclient.dll`. Without it, fdb uses its default library lookup. |

The library does not read `.env` itself. Load it with `python-dotenv` first:

```python
from dotenv import load_dotenv
load_dotenv()

from sitewatch_data_client import SiteWatchClient

with SiteWatchClient() as client:
    df = client.query("SELECT ID, Code, SiteName FROM V_Site WHERE ID = ?", [24])
    print(df)
```

!!! warning
    Never commit your `.env` file. The repo's `.gitignore` already excludes it. Error messages
    and `repr(config)` never include the password.

### Explicit Credentials

Pass credentials directly when you don't want environment variables. `host`, `database`,
`user` and `password` are then all required; leaving one out raises `ConfigurationError`.

```python
from sitewatch_data_client import SiteWatchClient

client = SiteWatchClient(
    host="your-query-server",
    database=r"E:\path\to\SiteWatch.FDB",
    user="REPORTS",
    password="change-me",
    port=3050,
    fb_client=r"C:\path\to\fbclient25\bin\fbclient.dll",
)
```

When you call `SiteWatchClient()` with no credentials, `port` and `fb_client` still override
the environment values if you pass them.

## Connecting

Use the context manager so the connection is always closed:

```python
with SiteWatchClient() as client:
    ...
```

Or manage it yourself. `connect()` returns the client, and `close()` is safe to call more than
once:

```python
client = SiteWatchClient().connect()
try:
    ...
finally:
    client.close()
```

The connection uses SQL dialect 1 and charset `NONE`. Text comes back right-trimmed (SiteWatch
`CHAR` columns are space-padded), and raw bytes are decoded as Windows-1252.

!!! info "Read-only transactions"
    Every statement runs in its own **read-only, read-committed** transaction, which is
    always rolled back afterwards. Firebird rejects any write in such a transaction, so
    nothing can be changed, even by mistake. You can see it for yourself:

    ```python
    client.query(
        "SELECT MON$READ_ONLY AS ro FROM MON$TRANSACTIONS "
        "WHERE MON$TRANSACTION_ID = CURRENT_TRANSACTION"
    )
    #    ro
    # 0   1
    ```

## Raw Queries with `query()`

`client.query(sql, params=None, *, max_rows=10000)` runs a single `SELECT` (or `WITH`) and
returns a pandas DataFrame with lower-case column names.

### Parameters

fdb uses `?` (qmark) placeholders. Always pass values as parameters:

```python
from datetime import date

df = client.query(
    "SELECT FIRST 10 ObjID, LogDate, Total FROM V_Sale "
    "WHERE Site = ? AND LogDate >= ? AND LogDate < ?",
    [24, date(2026, 10, 5), date(2026, 10, 12)],
)
```

`date` values are sent as midnight timestamps, because a dialect-1 `DATE` is a timestamp. Use a
half-open range (`LogDate >= start AND LogDate < end + 1 day`) to cover whole days.

!!! warning
    Never build SQL with f-strings or string concatenation from user input. Use `?`
    parameters. A hostile value passed as a parameter is just data.

### Row Cap

`max_rows` stops fetching after that many rows (default 10,000), so an accidental scan can't
flood memory. When the cap cuts the result short, a `WARNING` is logged on the
`sitewatch_data_client` logger and `df.attrs["truncated"]` is `True`.

```python
df = client.query(
    "SELECT ObjID, Total FROM V_Sale WHERE Site = ? AND LogDate >= ? AND LogDate < ?",
    [24, date(2026, 10, 5), date(2026, 10, 12)],
    max_rows=None,  # fetch everything
)
print(len(df), df.attrs["truncated"])
```

!!! warning "`max_rows` does not limit server load"
    The cap applies to the rows fetched, not to the work Firebird does. `SELECT COUNT(*) FROM
    V_Sale` returns one row after scanning all history, and `query()` has no statement timeout or
    cancel. `query()` logs a `WARNING` when a history view (`V_Sale`, `V_SaleItems`,
    `V_SalePasses`, `V_PunchInterval` ...) is read without a `WHERE` clause, and when the SQL
    calls `Arm_Monthly_Current_Status` (measured: about 8 minutes for one site),
    `Arm_Summary_For_Date_Range` or `CustClubPlan_Balances`. Bound every history query by
    `Site` and a date range.

### What the Guard Rejects

Before anything reaches the server, `query()` checks the SQL with a lexical guard and raises
`UnsafeQueryError` (a subclass of `ValidationError`) if it finds:

- anything other than a `SELECT` or `WITH` statement, or more than one statement;
- the keywords `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `EXECUTE`, `EXEC`, `CREATE`, `ALTER`,
  `DROP`, `RECREATE`, `GRANT`, `REVOKE`, `SET`, `COMMIT`, `ROLLBACK` or `INTO`;
- a stored procedure or UDF outside the **allowlist**. Only the 11 procedures DRB documents
  (`Arm_Monthly_Current_Status`, `Arm_SalePass_Member`, `Arm_SalePass_Members`,
  `Arm_Summary_For_Date_Range`, `CustClubPlan_Balances`, `CustPPD_Balances`,
  `CustPromo_Balances`, `Labor_Statistics`, `Prepaid_Balance`, `Prepaid_Balances`,
  `Prepaid_Series_Balances`) and the 30 documented `f_*` UDFs (`f_IsCompleted`,
  `f_StatusCode`, `f_ArmStatusName`, `f_SaleScr` ...) may be called. Every other routine the
  database declares is rejected, including the 15 data-changing procedures (`GEN_OBJID`,
  `DELETE_*_DATA`, `PREPAID_*` ...), the SSN functions, and UDFs such as `F_IBPASSWORD`,
  `F_QPUSHQUEUE` or `F_SPPUT` (UDFs run as native server code outside transaction control, so
  the read-only rollback cannot undo them). Any other `f_*(...)` call and any identifier shaped
  like `*_UPDATE_*` / `*_SET_*` are rejected too;
- `GEN_ID(...)` or `NEXT VALUE FOR`: generator increments are not transactional;
- sensitive columns: employee `SSNUM`, `PWORD`, `SECURITYCODE`, `DRVLNUM`, web password
  hash/salt, the `V_Site` credentials (`PASS`, `EMAILPASS`, `MPSPASSWORD` ...), card data and
  payment tokens (`MASKEDPAN`, `TOKEN`, `CVVR`, `AUTHCODE`);
- a select-list star (`SELECT *`, `alias.*`, `, *`, also written `FIRST(1)*`, `DISTINCT*` or
  `ALL*`) in a statement that touches a view holding secrets: `V_Employee`, `V_Site`,
  `V_SalePayments`, `V_SaleCharges`, `V_SaleArmPlan` (and a few views REPORTS cannot read
  today). List the columns you need instead.

Comments and string literals are ignored, so `WHERE Code = 'UPDATE'` is fine. Words inside
double quotes are still checked against the routine, column and view lists: SiteWatch's
dialect 1 reads `"SSNUM"` as a string literal, but a dialect-3 connection would read it as a
quoted identifier.

```python
from sitewatch_data_client import UnsafeQueryError

try:
    client.query("SELECT * FROM V_Employee")
except UnsafeQueryError as e:
    print(e)
# SELECT * / alias.* is not allowed with V_EMPLOYEE; list the columns you need explicitly.
```

The guard is best-effort. The read-only transaction is the real safety net.

### Load Etiquette

This is live production data. Keep queries light:

- Bound history views (`V_Sale`, `V_SaleItems`, `V_SalePasses`, `V_PunchInterval` ...) by `Site`
  **and** a date range.
- Use `SELECT FIRST n` (and `SKIP n`); `LIMIT` and `TOP` don't exist in Firebird.
- Avoid `IN (SELECT ...)`; use a derived table plus `JOIN`.
- Completed sales are `f_IsCompleted(Status) = 1`. Cast money with `CAST(... AS NUMERIC(12,2))`
  and still round in Python: in dialect 1 the cast can return values like `2112.090000000001`.
- Run one query at a time. The client never runs statements in parallel.

## Recipes

### Completed sales count and total for one site and week

```python
with SiteWatchClient() as client:
    df = client.query(
        "SELECT COUNT(*) AS n, CAST(SUM(Total) AS NUMERIC(12,2)) AS total "
        "FROM V_Sale WHERE Site = ? AND LogDate >= ? AND LogDate < ? "
        "AND f_IsCompleted(Status) = 1",
        [24, date(2026, 10, 5), date(2026, 10, 12)],
    )
    print(int(df.loc[0, "n"]), round(float(df.loc[0, "total"]), 2))
```

### ICON site codes

```python
df = client.query(
    "SELECT ID, Code, SiteName FROM V_Site WHERE ID IN (?, ?, ?, ?, ?, ?) ORDER BY ID",
    [5, 13, 18, 20, 24, 25],
)
```

The site code (for example `ICONTN-030`) is not the site ID.

## Error Handling

| Exception | When |
|-----------|------|
| `ConfigurationError` | Missing environment variables, a non-integer port, or incomplete explicit credentials. |
| `ConnectionError` | `connect()` failed, or a query ran before `connect()`. |
| `UnsafeQueryError` | The `query()` guard rejected the SQL. Nothing was sent to the server. |
| `QueryError` | Firebird rejected or failed the statement. The message has the first 100 characters of the SQL but never the parameters. |

All of them derive from `SiteWatchError`.

```python
from sitewatch_data_client import QueryError, SiteWatchClient

with SiteWatchClient() as client:
    try:
        client.query("SELECT NoSuchColumn FROM V_Site WHERE ID = ?", [24])
    except QueryError as e:
        print(f"Query failed: {e}")
    # The connection is still usable after a failed statement.
```
