"""Lexical read-only guard for ``SiteWatchClient.query()``.

This is a best-effort, deliberately conservative check. The real safety net is the
read-only transaction every statement runs in (see ``_connection.py``). Library-owned SQL in
the resources does not pass through this guard.

Procedures and UDFs are checked against an **allowlist**: only the 11 stored procedures and
30 UDFs that DRB documents may be called. Every other procedure or UDF the SiteWatch database
declares is rejected, because UDFs run as native server-side code outside transaction control
and the undocumented procedures have unknown side effects.
"""

from __future__ import annotations

import re

from sitewatch_data_client._exceptions import UnsafeQueryError

#: The stored procedures documented by DRB. These are the only ones query() may call.
ALLOWED_PROCEDURES: frozenset[str] = frozenset(
    {
        "ARM_MONTHLY_CURRENT_STATUS",
        "ARM_SALEPASS_MEMBER",
        "ARM_SALEPASS_MEMBERS",
        "ARM_SUMMARY_FOR_DATE_RANGE",
        "CUSTCLUBPLAN_BALANCES",
        "CUSTPPD_BALANCES",
        "CUSTPROMO_BALANCES",
        "LABOR_STATISTICS",
        "PREPAID_BALANCE",
        "PREPAID_BALANCES",
        "PREPAID_SERIES_BALANCES",
    }
)

#: The UDFs documented by DRB (plus the read-only ``RDB$GET_CONTEXT`` built-in).
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "F_ABSDBL",
        "F_ABSINT",
        "F_ADDMONTH",
        "F_ADDYEAR",
        "F_ARMSTATUSNAME",
        "F_DAYOFMONTH",
        "F_DAYOFWEEK",
        "F_DAYOFYEAR",
        "F_ISCOMPLETED",
        "F_ISINCOMPLETE",
        "F_LEFT",
        "F_LRTRIM",
        "F_LTRIM",
        "F_MID",
        "F_MODULO",
        "F_MONTH",
        "F_NEXTBIRTHDAY",
        "F_PADLEFT",
        "F_PADRIGHT",
        "F_PPDSERIESNUM",
        "F_RIGHT",
        "F_RTRIM",
        "F_SALESCR",
        "F_SALEUNSCR",
        "F_SCENTRYMODE",
        "F_SCFLAGCHARS",
        "F_SERVICEEXIST",
        "F_STATUSCODE",
        "F_STRIPTIME",
        "F_YEAR",
        "RDB$GET_CONTEXT",
    }
)

#: Every stored procedure the SiteWatch database declares (``RDB$PROCEDURES``, taken from
#: the schema catalog). Anything here that is not in ``ALLOWED_PROCEDURES`` is rejected.
KNOWN_PROCEDURES: frozenset[str] = frozenset(
    {
        "ARM_DETAILS_FOR_DATE_RANGE",
        "ARM_DETAILS_FOR_PLAN",
        "ARM_MEMBER_CHANGES",
        "ARM_MONTHLY_CURRENT_STATUS",
        "ARM_PAYMENT_VALIDATION",
        "ARM_RECHARGEABLE_SALEPASSES",
        "ARM_RECHARGING_CUSTOMERS",
        "ARM_SALEPASS_GROUP_MEMBERS",
        "ARM_SALEPASS_LEADER_CHANGE",
        "ARM_SALEPASS_MEMBER",
        "ARM_SALEPASS_MEMBERS",
        "ARM_SALEPASS_MEMBER_SITED",
        "ARM_SUMMARY_FOR_DATE_RANGE",
        "ARM_UNIT_CURRENT_STATUS",
        "CUSTCLUBPLAN_BALANCES",
        "CUSTPPD_BALANCES",
        "CUSTPPD_UPDATE_DAY_BALANCES",
        "CUSTPROMO_BALANCES",
        "CUSTPROMO_UPDATE_BALANCE",
        "CUSTPROMO_UPDATE_DAY_BALANCES",
        "DELETE_INVENTORY_DATA",
        "DELETE_SALE_DATA",
        "DELETE_SECURITY_DATA",
        "DELETE_SPECIFIC_SALE",
        "DELETE_TRAINING_SALES",
        "EMPLOYEELASTPUNCHDATA",
        "EMPLOYEESWORKING",
        "GEN_OBJID",
        "GETVERSIONINFO",
        "GET_FREQUENT_CUSTOMER_DATA",
        "ITEMDETAILDATA",
        "LABOR_STATISTICS",
        "PREPAID_ACTIVATE_CODES",
        "PREPAID_BALANCE",
        "PREPAID_BALANCES",
        "PREPAID_CODES_IN_RANGE",
        "PREPAID_CREATE_TEMP_CODES",
        "PREPAID_CREDITS",
        "PREPAID_DEACTIVATE_CODES",
        "PREPAID_SERIES_BALANCES",
        "PREPAID_SET_BALANCE",
        "PREPAID_UPDATE_BALANCE",
        "PREPAID_UPDATE_DAY_BALANCES",
        "PREPAID_USED",
        "SALEPROFITCENTERS",
        "SERVICE_CONTROL_COUNTS",
    }
)

#: Every UDF the SiteWatch database declares (``RDB$FUNCTIONS``, taken from the schema
#: catalog). Anything here that is not in ``ALLOWED_FUNCTIONS`` is rejected.
KNOWN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "F_ABSDBL", "F_ABSINT", "F_ADDMONTH", "F_ADDYEAR", "F_AGEINDAYS",
        "F_AGEINDAYSTHRESHOLD", "F_AGEINMONTHS", "F_AGEINMONTHSTHRESHOLD", "F_AGEINWEEKS",
        "F_AGEINWEEKSTHRESHOLD", "F_ARMSTATUSNAME", "F_BLOBASPCHAR", "F_BLOBCHECKSUM",
        "F_BLOBCOMPARE", "F_BLOBLEFT", "F_BLOBLINE", "F_BLOBMAXSEGMENTLENGTH", "F_BLOBMID",
        "F_BLOBRIGHT", "F_BLOBSEGMENTCOUNT", "F_BLOBSIZE", "F_CDOWLONG", "F_CDOWSHORT",
        "F_CHARACTER", "F_CLOSEDEBUGGEROUTPUT", "F_CMONTHLONG", "F_CMONTHSHORT",
        "F_COMBINEDVEHICLESTRING", "F_COMBINENAMEFROMPARTS", "F_CONVERTFLAGS",
        "F_CONVERTLEADER", "F_CRLF", "F_DATEFROMMMYY", "F_DATEFROMMOMENT", "F_DATETODOUBLE",
        "F_DAYOFMONTH", "F_DAYOFWEEK", "F_DAYOFYEAR", "F_DEBUG", "F_DOLLARVAL",
        "F_DOUBLETODATE", "F_DTCONDINTERVAL", "F_DTINTERVAL", "F_ENCRYPTEMPPASS",
        "F_FASTPASSUNSCR", "F_FINDFIRSTWORD", "F_FINDNEXTWORD", "F_FINDNTHWORD",
        "F_FIXEDPOINT", "F_FORMATEMPNAMENUM", "F_GENERATEFORMATTEDNAME",
        "F_GETCURRENTWORDINDEX", "F_GETDECRYPTEDSSN", "F_GETENCRYPTEDSSN", "F_HASANYSERVICE",
        "F_HASOUTPUT", "F_HASSCFLAG", "F_HASSERVICETYPE", "F_HOUR", "F_HOURFROMMINUTE",
        "F_IBPASSWORD", "F_IBTEMPPATH", "F_INTCOMPARE", "F_INTMAX", "F_INTMIN",
        "F_ISADJUSTED", "F_ISCOMPLETED", "F_ISDIVISIBLEBY", "F_ISENTERED", "F_ISINCOMPLETE",
        "F_ISNOTZERO", "F_ISREVERSED", "F_ISSERVICEONLY", "F_ISVOID", "F_ISZERO", "F_LEFT",
        "F_LRTRIM", "F_LTRIM", "F_MARKSERVICEDONE", "F_MARKSERVICENEEDED", "F_MAXDATE",
        "F_MID", "F_MINDATE", "F_MINUTESREMAINING", "F_MODULO", "F_MOMENTFROMDATE",
        "F_MONTH", "F_NEEDSSERVICETYPE", "F_NEXTBIRTHDAY", "F_PADLEFT", "F_PADRIGHT",
        "F_PHONEREVERSE", "F_POWER", "F_PPDCODE", "F_PPDSEQNUM", "F_PPDSERIESNUM",
        "F_PPDSERIESSEQNUM", "F_PROPERCASE", "F_QPUSHQUEUE", "F_QUARTER", "F_QUOTESTRING",
        "F_REPLACESIMILARCHARS", "F_RIGHT", "F_ROUNDFLOAT", "F_RTRIM", "F_SALEITEMSFLAGS",
        "F_SALESCR", "F_SALEUNSCR", "F_SCENTRYMODE", "F_SCFLAGCHARS", "F_SERVICEEXIST",
        "F_SETDEBUGGEROUTPUT", "F_SITESTATUS", "F_SPERR", "F_SPGET", "F_SPINIT", "F_SPPUT",
        "F_STATUSCODE", "F_STRBLOB", "F_STRIPDATE", "F_STRIPSTRING", "F_STRIPTIME",
        "F_SUBSTR", "F_TRUNCATE", "F_VALIDATENAMEFORMAT", "F_WEEKOFYEAR", "F_WOY", "F_YEAR",
        "F_YEAROFYEAR", "RDB$GET_CONTEXT", "RDB$SET_CONTEXT",
    }
)  # fmt: skip

#: Procedures that change data (DRB "never call" list). They are rejected by the allowlist
#: as well; this set gives them a specific error message.
BLOCKED_PROCEDURES: frozenset[str] = frozenset(
    {
        "DELETE_INVENTORY_DATA",
        "DELETE_SALE_DATA",
        "DELETE_SECURITY_DATA",
        "DELETE_SPECIFIC_SALE",
        "DELETE_TRAINING_SALES",
        "GEN_OBJID",
        "PREPAID_ACTIVATE_CODES",
        "PREPAID_CREATE_TEMP_CODES",
        "PREPAID_DEACTIVATE_CODES",
        "PREPAID_SET_BALANCE",
        "PREPAID_UPDATE_BALANCE",
        "PREPAID_UPDATE_DAY_BALANCES",
        "CUSTPPD_UPDATE_DAY_BALANCES",
        "CUSTPROMO_UPDATE_BALANCE",
        "CUSTPROMO_UPDATE_DAY_BALANCES",
    }
)

#: Declared UDFs outside the allowlist, plus ``GEN_ID`` (generator increments are not
#: transactional, so the read-only rollback does not undo them).
BLOCKED_FUNCTIONS: frozenset[str] = frozenset((KNOWN_FUNCTIONS - ALLOWED_FUNCTIONS) | {"GEN_ID"})

#: Every routine query() rejects: declared procedures and UDFs outside the allowlists.
BLOCKED_ROUTINES: frozenset[str] = frozenset(
    (KNOWN_PROCEDURES - ALLOWED_PROCEDURES) | BLOCKED_FUNCTIONS | BLOCKED_PROCEDURES
)

#: Statement keywords that are never allowed (whole words, outside string literals).
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "EXECUTE",
        "EXEC",
        "CREATE",
        "ALTER",
        "DROP",
        "RECREATE",
        "GRANT",
        "REVOKE",
        "SET",
        "COMMIT",
        "ROLLBACK",
        "INTO",
    }
)

#: Columns that must never be selected: employee secrets, credentials, card data and tokens.
SENSITIVE_COLUMNS: frozenset[str] = frozenset(
    {
        # V_Employee
        "SSNUM",
        "PWORD",
        "SECURITYCODE",
        "DRVLNUM",
        "WEBPASSWORDHASH",
        "WEBPASSWORDSALT",
        # V_Site credentials
        "PASS",
        "EMAILPASS",
        "MPSPASSWORD",
        "SECRETACCESSKEY",
        "ACCESSKEYID",
        "FACEBOOKAPPSECRET",
        # Cardholder data and payment tokens (V_SalePayments, V_SaleCharges, V_SaleArmPlan)
        "MASKEDPAN",
        "TOKEN",
        "CVVR",
        "AUTHCODE",
        # Other secrets (not granted to REPORTS today; blocked in case grants change)
        "GOOGLEAUTHTOKEN",
        "ENCRYPTIONKEY",
    }
)

#: Views that may not be read with ``SELECT *`` or ``alias.*`` because they hold secrets.
NO_STAR_VIEWS: frozenset[str] = frozenset(
    {
        "V_EMPLOYEE",
        "V_SITE",
        "V_SALEPAYMENTS",
        "V_SALECHARGES",
        "V_SALEARMPLAN",
        "V_EMPLOYEESESSION",
        "V_EXPORTCONFIG",
        "V_CHAINSPECIFIC",
    }
)

#: Large history views. A query on one of these without a ``WHERE`` clause scans all history.
HISTORY_VIEWS: frozenset[str] = frozenset(
    {
        "V_SALE",
        "V_SALEITEMS",
        "V_SALESTATS",
        "V_SALECHARGES",
        "V_SALEPASSES",
        "V_SALEFACTS",
        "V_SALEPAYMENTS",
        "V_PUNCHINTERVAL",
        "V_PUNCHSTATS",
    }
)

#: Procedures known to run for minutes on the shared production server.
HEAVY_PROCEDURES: frozenset[str] = frozenset(
    {"ARM_MONTHLY_CURRENT_STATUS", "ARM_SUMMARY_FOR_DATE_RANGE", "CUSTCLUBPLAN_BALANCES"}
)

# One left-to-right pass so a quote inside a comment, or "--" inside a literal, is handled.
# Dialect 1 treats both '...' and "..." as string literals; '' / "" are escaped quotes.
_TOKENS = re.compile(
    r"(?P<lit>'(?:[^']|'')*')"
    r"|(?P<dq>\"(?:[^\"]|\"\")*\")"
    r"|(?P<block>/\*.*?(?:\*/|\Z))"
    r"|(?P<line>--[^\n]*)",
    re.DOTALL,
)
_WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_LEX = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*|\d+(?:\.\d*)?|:[A-Za-z_]\w*|\S")
_WRITE_PATTERN = re.compile(r"^\w*_(UPDATE|SET)_\w*$")
_NEXT_VALUE_FOR = re.compile(r"\bNEXT\s+VALUE\s+FOR\b", re.IGNORECASE)
_UDF_CALL = re.compile(r"(?<![\w$])(F_[A-Za-z0-9_$]+)\s*\(", re.IGNORECASE)


def _strip(sql: str) -> tuple[str, set[str]]:
    """Remove comments and replace string literals with an empty literal.

    Returns the cleaned SQL and the upper-cased words found inside double quotes. Under the
    SiteWatch dialect 1 ``"..."`` is a string literal (``SELECT "ID" FROM V_Site`` returns the
    text ``ID``), but under dialect 3 it is a quoted identifier (``e."SSNUM"``). The words are
    therefore still checked against the routine, column and view denylists, so a later
    dialect-3 connection cannot quietly open a bypass.
    """
    quoted: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        if m.group("lit") is not None:
            return " '' "
        if m.group("dq") is not None:
            inner = m.group("dq")[1:-1].replace('""', '"')
            quoted.add(inner.strip().upper())
            quoted.update(w.upper() for w in _WORD.findall(inner))
            return " '' "
        return " "

    return _TOKENS.sub(repl, sql), quoted


def _skip_argument(tokens: list[str], i: int) -> int:
    """Skip one ``FIRST``/``SKIP`` argument (``n``, ``?``, ``:p`` or ``(expr)``)."""
    if i >= len(tokens):
        return i
    if tokens[i] != "(":
        return i + 1
    depth = 0
    while i < len(tokens):
        if tokens[i] == "(":
            depth += 1
        elif tokens[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _has_star_select(body: str) -> bool:
    """True if a select-list star (``SELECT *``, ``alias.*`` or ``, *``) appears.

    Tokenizes rather than pattern-matching the select list, so ``FIRST(1)*``, ``DISTINCT*``,
    ``ALL*`` and newlines or tabs before ``*`` are all recognised, while arithmetic such as
    ``a * b`` and ``COUNT(*)`` is not.
    """
    tokens = _LEX.findall(body)
    upper = [t.upper() for t in tokens]
    for i, tok in enumerate(tokens):
        if tok == "*" and i > 0 and upper[i - 1] in (",", "."):
            return True
        if upper[i] != "SELECT":
            continue
        j = i + 1
        if j < len(upper) and upper[j] == "FIRST":
            j = _skip_argument(tokens, j + 1)
        if j < len(upper) and upper[j] == "SKIP":
            j = _skip_argument(tokens, j + 1)
        if j < len(upper) and upper[j] in ("DISTINCT", "ALL"):
            j += 1
        if j < len(tokens) and tokens[j] == "*":
            return True
    return False


def assert_read_only_sql(sql: str) -> None:
    """Reject SQL that could write data, call an unsafe routine or read secrets.

    Rules (case-insensitive, outside comments and string literals):

    * The first keyword must be ``SELECT`` or ``WITH``.
    * One statement only: a ``;`` may be followed by whitespace only.
    * No DML/DDL/transaction keywords (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``,
      ``EXECUTE``, ``EXEC``, ``CREATE``, ``ALTER``, ``DROP``, ``RECREATE``, ``GRANT``,
      ``REVOKE``, ``SET``, ``COMMIT``, ``ROLLBACK``, ``INTO``).
    * Procedures and UDFs are allowlisted: only the 11 DRB-documented stored procedures and
      the 30 documented ``f_*`` UDFs. Any other routine the database declares, any other
      ``f_*(...)`` call, identifiers like ``*_UPDATE_*`` / ``*_SET_*``, ``GEN_ID`` and
      ``NEXT VALUE FOR`` are rejected.
    * No sensitive column (employee secrets, ``V_Site`` credentials, card data, payment
      tokens).
    * No ``SELECT *`` or ``alias.*`` in a statement that references a view holding secrets
      (``V_Employee``, ``V_Site``, ``V_SalePayments``, ``V_SaleCharges``, ``V_SaleArmPlan``
      and a few views REPORTS cannot read today).
    * Words inside double quotes are checked against the routine, column and view lists
      too. Under dialect 1 they are string literals, but under dialect 3 they would be
      quoted identifiers.

    Args:
        sql: The SQL text to check.

    Raises:
        UnsafeQueryError: With the reason the statement was rejected.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeQueryError("SQL must be a non-empty string.")

    cleaned, quoted = _strip(sql)
    body = cleaned.strip()
    if not body:
        raise UnsafeQueryError("SQL contains only comments.")

    first = _WORD.match(body)
    if first is None or first.group(0).upper() not in ("SELECT", "WITH"):
        raise UnsafeQueryError("Only SELECT or WITH statements are allowed in query().")

    semi = body.find(";")
    if semi != -1 and body[semi + 1 :].strip():
        raise UnsafeQueryError("Only one statement is allowed (found text after ';').")

    words = {w.upper() for w in _WORD.findall(body)}
    ident_words = words | quoted

    bad_kw = sorted(words & FORBIDDEN_KEYWORDS)
    if bad_kw:
        raise UnsafeQueryError(f"Forbidden keyword(s) in query(): {', '.join(bad_kw)}.")

    if _NEXT_VALUE_FOR.search(body):
        raise UnsafeQueryError(
            "NEXT VALUE FOR is not allowed: generator increments are not transactional."
        )

    bad_proc = sorted(ident_words & BLOCKED_PROCEDURES)
    if bad_proc:
        raise UnsafeQueryError(
            f"Blocked data-changing procedure(s): {', '.join(bad_proc)}. Never call these."
        )
    pattern_hits = sorted(w for w in ident_words if _WRITE_PATTERN.match(w))
    if pattern_hits:
        raise UnsafeQueryError(
            f"Identifier(s) look like data-changing procedures: {', '.join(pattern_hits)}."
        )

    unknown_udfs = {m.group(1).upper() for m in _UDF_CALL.finditer(body)} - ALLOWED_FUNCTIONS
    blocked = sorted((ident_words & BLOCKED_ROUTINES) | unknown_udfs)
    if blocked:
        raise UnsafeQueryError(
            f"Blocked function/procedure(s): {', '.join(blocked)}. query() allows only the "
            "DRB-documented stored procedures and f_* UDFs."
        )

    bad_cols = sorted(ident_words & SENSITIVE_COLUMNS)
    if bad_cols:
        raise UnsafeQueryError(f"Sensitive column(s) may not be selected: {', '.join(bad_cols)}.")

    star_views = sorted(ident_words & NO_STAR_VIEWS)
    if star_views and _has_star_select(body):
        raise UnsafeQueryError(
            f"SELECT * / alias.* is not allowed with {', '.join(star_views)}; "
            "list the columns you need explicitly."
        )


def query_load_warnings(sql: str) -> list[str]:
    """Return warnings for SQL that is likely to load the shared server heavily.

    Nothing is rejected. This flags a history view read without a ``WHERE`` clause, and the
    procedures known to run for minutes.

    Args:
        sql: SQL that already passed ``assert_read_only_sql``.

    Returns:
        Human-readable warnings (empty when nothing stands out).
    """
    cleaned, _ = _strip(sql)
    words = {w.upper() for w in _WORD.findall(cleaned)}
    warnings: list[str] = []
    history = sorted(words & HISTORY_VIEWS)
    if history and "WHERE" not in words:
        warnings.append(
            f"query() reads {', '.join(history)} without a WHERE clause, which scans all "
            "history on the shared production server. Bound it by Site and a date range; "
            "max_rows limits only the rows fetched, not the server work."
        )
    heavy = sorted(words & HEAVY_PROCEDURES)
    if heavy:
        warnings.append(
            f"query() calls {', '.join(heavy)}, which can run for many minutes on the shared "
            "server (about 8 minutes for one site has been measured). Run it off-hours."
        )
    return warnings
