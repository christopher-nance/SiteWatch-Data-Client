# Exceptions

All exceptions inherit from `SiteWatchError`, so one handler catches every client error:

```python
from sitewatch_data_client import SiteWatchClient, SiteWatchError

try:
    with SiteWatchClient() as client:
        df = client.query("SELECT ID, Code FROM V_Site WHERE ID = ?", [24])
except SiteWatchError as e:
    print(f"Client error: {e}")
```

## Exception Hierarchy

```
SiteWatchError
├── ConfigurationError
├── ConnectionError
├── QueryError
└── ValidationError
    └── UnsafeQueryError
```

Messages never include the password or query parameters. `QueryError` includes a preview
of the first 100 characters of the SQL.

## SiteWatchError

::: sitewatch_data_client.SiteWatchError

## ConfigurationError

::: sitewatch_data_client.ConfigurationError

## ConnectionError

::: sitewatch_data_client.ConnectionError

## QueryError

::: sitewatch_data_client.QueryError

## ValidationError

::: sitewatch_data_client.ValidationError

## UnsafeQueryError

::: sitewatch_data_client.UnsafeQueryError
