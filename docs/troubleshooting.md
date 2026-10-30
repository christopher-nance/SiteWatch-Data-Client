# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ConfigurationError: Missing required environment variables ...` | `.env` not loaded or keys missing | Call `load_dotenv()` before creating the client, or pass the settings to the constructor. Compare with `.env.example`. |
| `ConnectionError ... Unable to complete network request to host` (SQLCODE -902) | Port 3050 can't be reached from your network | Run `Test-NetConnection <host> -Port 3050`. `False` means a network or firewall problem between you and the Query Server (your firewall, the site's Comcast gateway or SonicWall, or the server's Windows Firewall). Contact the repo owner or DRB. |
| `ConnectionError ... Your user name and password are not defined` | Wrong `SITEWATCH_USER` / `SITEWATCH_PASSWORD` | Check the credentials. The REPORTS password may have been rotated. |
| `ConnectionError ... I/O error ... CreateFile` / database not found | Wrong `SITEWATCH_DATABASE` path | Use the **server-side** path to `SiteWatch.FDB` (it starts with a drive letter on the server, not on your PC). |
| `OSError` / "fbclient library not found" / `%1 is not a valid Win32 application` | Firebird client missing, or 32-bit vs 64-bit mismatch | Point `SITEWATCH_FB_CLIENT` at the **x64** Firebird 2.5 `fbclient.dll`, matching 64-bit Python. |
| `connection rejected by remote interface` | Connected to a different Firebird server (wrong host or port) | Check `SITEWATCH_HOST` and `SITEWATCH_PORT`. |
| `QueryError ... no permission for read/select access` (SQLCODE -551) | REPORTS can only read 117 of the 237 `V_*` views | Use a view REPORTS can read (the `catalog.json` notes list them), or ask DRB for a grant. |
| `UnsafeQueryError` from `query()` | The statement isn't a single read-only `SELECT`, uses a blocked procedure, function or sensitive column, or uses `SELECT *` on a protected view | Select explicit columns and use the documented procedures and `f_*` functions only. |
| `ValidationError: Date range of N days exceeds the M-day limit for this method` | Date range over the method's cap | Split the period into chunks (see [Recipes](recipes.md)). |
| Empty results for recent dates | Data not replicated to the central database yet | Check `client.sites.data_freshness()`. |
| A query takes minutes | Unbounded history scan or a heavy stored procedure on the shared server | Add `Site` and date filters and `FIRST n`. Run heavy procedures (e.g. `Arm_Monthly_Current_Status`) off-hours only. |
| Numbers don't match a SiteWatch report | Different definitions (completed vs incomplete sales, recharges, office accounts, per-site overtime) | Read the "Business rules" notes in the resource guide, then compare against the matching SiteWatch report. |

## Checking connectivity from PowerShell

```powershell
Test-NetConnection <SITEWATCH_HOST> -Port 3050 | Select RemoteAddress, TcpTestSucceeded
```

## Getting help

Open an issue in this repository with the full error message, with the password removed. Include
the method call and date range you used.
