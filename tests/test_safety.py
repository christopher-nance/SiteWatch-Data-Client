"""Tests for the query() read-only guard."""

import pytest

from sitewatch_data_client._exceptions import UnsafeQueryError, ValidationError
from sitewatch_data_client._safety import (
    ALLOWED_FUNCTIONS,
    ALLOWED_PROCEDURES,
    BLOCKED_PROCEDURES,
    BLOCKED_ROUTINES,
    KNOWN_FUNCTIONS,
    KNOWN_PROCEDURES,
    SENSITIVE_COLUMNS,
    assert_read_only_sql,
    query_load_warnings,
)

ALLOWED = [
    "SELECT ID, Code FROM V_Site WHERE ID = ?",
    "select first 10 ObjID, LogDate FROM V_Sale WHERE Site = ? AND LogDate >= ?",
    "  \n-- leading comment\nSELECT 1 FROM RDB$DATABASE",
    "/* block */ SELECT 1 FROM RDB$DATABASE;",
    "SELECT 1 FROM RDB$DATABASE;   \n  ",
    "WITH x AS (SELECT 1 AS a FROM RDB$DATABASE) SELECT a FROM x",
    "SELECT COUNT(*) FROM V_Sale WHERE Site = ? AND LogDate >= ? AND LogDate < ?",
    "SELECT * FROM Labor_Statistics(?, ?, ?, NULL, 1, NULL, NULL, NULL, NULL, NULL)",
    "SELECT s.* FROM V_Sale s WHERE s.Site = ?",
    # Keywords inside string literals or comments are ignored.
    "SELECT 'DELETE FROM V_Sale' AS txt FROM RDB$DATABASE",
    "SELECT 1 FROM RDB$DATABASE -- UPDATE something",
    'SELECT "insert" FROM RDB$DATABASE',
    "SELECT COUNT(*) FROM V_Employee WHERE Site = ?",
    "SELECT e.ObjID, e.FirstName FROM V_Employee e WHERE e.Site = ?",
    "SELECT ID, Code, SiteName FROM V_Site",
    "SELECT Settled FROM V_SalePasses",
]

REJECTED = [
    ("", "non-empty"),
    ("   -- only a comment", "only comments"),
    ("INSERT INTO V_Sale (ObjID) VALUES (1)", "SELECT or WITH"),
    ("UPDATE V_Sale SET Total = 0", "SELECT or WITH"),
    ("DELETE FROM V_Sale", "SELECT or WITH"),
    ("EXECUTE PROCEDURE GEN_OBJID", "SELECT or WITH"),
    ("EXECUTE BLOCK AS BEGIN END", "SELECT or WITH"),
    ("SELECT 1 FROM RDB$DATABASE; DELETE FROM V_Sale", "one statement"),
    ("SELECT 1 FROM RDB$DATABASE; SELECT 2 FROM RDB$DATABASE", "one statement"),
    ("SELECT ObjID INTO :x FROM V_Sale", "INTO"),
    ("SELECT 1 FROM RDB$DATABASE WHERE 1 = 1 FOR UPDATE", "UPDATE"),
    ("WITH x AS (SELECT 1 a FROM RDB$DATABASE) DELETE FROM V_Sale", "DELETE"),
    ("SELECT * FROM PREPAID_SET_BALANCE(1, 2)", "PREPAID_SET_BALANCE"),
    ("SELECT * FROM SOMETHING_UPDATE_TOTALS(1)", "SOMETHING_UPDATE_TOTALS"),
    ("SELECT * FROM X_SET_FLAG(1)", "X_SET_FLAG"),
    ("SELECT f_GetDecryptedSSN(SSN) FROM RDB$DATABASE", "F_GETDECRYPTEDSSN"),
    ("SELECT f_GetEncryptedSSN('x') FROM RDB$DATABASE", "F_GETENCRYPTEDSSN"),
    ("SELECT ObjID, SSNUM FROM V_Employee", "SSNUM"),
    ("SELECT pword FROM V_Employee", "PWORD"),
    ("SELECT SecurityCode, DrvLNum FROM V_Employee", "DRVLNUM"),
    ("SELECT WebPasswordHash FROM V_Employee", "WEBPASSWORDHASH"),
    ("SELECT * FROM V_Employee", "V_EMPLOYEE"),
    ("SELECT e.* FROM V_Employee e", "V_EMPLOYEE"),
    ("SELECT FIRST 1 * FROM V_Site", "V_SITE"),
    ("select ID, EmailPass from V_Site", "EMAILPASS"),
    ("SELECT Pass FROM V_Site", "PASS"),
    ("SELECT MpsPassword, SecretAccessKey, AccessKeyID FROM V_Site", "MPSPASSWORD"),
    ("SELECT FacebookAppSecret FROM V_Site", "FACEBOOKAPPSECRET"),
    ("SELECT MaskedPan FROM V_SalePayments", "MASKEDPAN"),
    ("SELECT * FROM V_SalePayments WHERE Site = ?", "V_SALEPAYMENTS"),
    ("SELECT c.* FROM V_SaleCharges c", "V_SALECHARGES"),
    ("SELECT s.ObjID, * FROM V_Sale s JOIN V_Site x ON x.ID = s.Site", "V_SITE"),
    ("/* SELECT */ DROP TABLE X", "SELECT or WITH"),
    ("SELECT 1 FROM RDB$DATABASE /* unterminated", None),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    assert_read_only_sql(sql)


@pytest.mark.parametrize(("sql", "reason"), REJECTED)
def test_rejected(sql, reason):
    if reason is None:
        # An unterminated block comment swallows the rest; it must still be a SELECT.
        assert_read_only_sql(sql)
        return
    with pytest.raises(UnsafeQueryError, match=reason):
        assert_read_only_sql(sql)


@pytest.mark.parametrize("proc", sorted(BLOCKED_PROCEDURES))
def test_every_blocked_procedure_rejected(proc):
    with pytest.raises(UnsafeQueryError):
        assert_read_only_sql(f"SELECT * FROM {proc.lower()}(1)")


@pytest.mark.parametrize("col", sorted(SENSITIVE_COLUMNS))
def test_every_sensitive_column_rejected(col):
    with pytest.raises(UnsafeQueryError):
        assert_read_only_sql(f"SELECT {col} FROM V_Anything")


def test_unsafe_is_validation_error():
    assert issubclass(UnsafeQueryError, ValidationError)


def test_non_string_rejected():
    with pytest.raises(UnsafeQueryError):
        assert_read_only_sql(None)  # type: ignore[arg-type]


# --- Review hardening -------------------------------------------------------------------

STAR_BYPASSES = [
    "SELECT FIRST(1)* FROM V_Employee",
    "SELECT DISTINCT* FROM V_Employee",
    "SELECT ALL* FROM V_Site",
    "SELECT FIRST 1 SKIP(0)* FROM V_Employee",
    "SELECT FIRST ? * FROM V_Employee",
    "SELECT FIRST (1 + 1) SKIP ? DISTINCT * FROM V_Site",
    "SELECT\n* FROM V_Employee",
    "SELECT\t* FROM V_Site",
    "select first(1)\n*from v_employee",
    "SELECT x FROM (SELECT * FROM V_Employee) x",
    "SELECT e.\n* FROM V_Employee e",
    "SELECT ID,*FROM V_Site",
    "SELECT * FROM V_SaleArmPlan WHERE Sale = ?",
]


@pytest.mark.parametrize("sql", STAR_BYPASSES)
def test_star_select_variants_rejected(sql):
    with pytest.raises(UnsafeQueryError, match=r"SELECT \*"):
        assert_read_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT Price * Qty AS amt FROM V_SaleCharges WHERE Sale = ?",
        "SELECT FIRST 5 Amount*2 FROM V_SalePayments WHERE Sale = ?",
        "SELECT FIRST 1 2 * Amount FROM V_SalePayments WHERE Sale = ?",
        "SELECT COUNT(*) FROM V_Site",
        "SELECT FIRST(1)* FROM RDB$DATABASE",
    ],
)
def test_arithmetic_and_count_star_still_allowed(sql):
    assert_read_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT F_IBPASSWORD() FROM RDB$DATABASE",
        "SELECT F_QPUSHQUEUE(1) FROM RDB$DATABASE",
        "SELECT F_SPPUT(1,2) FROM RDB$DATABASE",
        "SELECT F_SPINIT() FROM RDB$DATABASE",
        "SELECT F_ENCRYPTEMPPASS('x') FROM RDB$DATABASE",
        "SELECT F_MARKSERVICEDONE(1) FROM RDB$DATABASE",
        "SELECT F_MARKSERVICENEEDED(1) FROM RDB$DATABASE",
        "SELECT F_SETDEBUGGEROUTPUT('x') FROM RDB$DATABASE",
        "SELECT F_DEBUG('x') FROM RDB$DATABASE",
        "SELECT f_IsVoid(Status) FROM V_Sale WHERE Site = ?",
        "SELECT F_SOMETHINGNEW(1) FROM RDB$DATABASE",
        "SELECT RDB$SET_CONTEXT('USER_SESSION', 'x', 1) FROM RDB$DATABASE",
        "SELECT * FROM ARM_SALEPASS_LEADER_CHANGE(?)",
        "SELECT * FROM EmployeesWorking(?)",
        "SELECT * FROM ARM_PAYMENT_VALIDATION(?)",
        "SELECT * FROM Arm_Details_For_Date_Range(?, ?)",
    ],
)
def test_undocumented_routines_rejected(sql):
    with pytest.raises(UnsafeQueryError, match="Blocked function/procedure"):
        assert_read_only_sql(sql)


@pytest.mark.parametrize("fn", sorted(ALLOWED_FUNCTIONS - {"RDB$GET_CONTEXT"}))
def test_documented_udfs_allowed(fn):
    assert_read_only_sql(f"SELECT {fn.lower()}(1) FROM RDB$DATABASE")


@pytest.mark.parametrize("proc", sorted(ALLOWED_PROCEDURES))
def test_documented_procedures_allowed(proc):
    assert_read_only_sql(f"SELECT FIRST 1 * FROM {proc}(?, ?)")


@pytest.mark.parametrize("routine", sorted(BLOCKED_ROUTINES))
def test_every_blocked_routine_rejected(routine):
    with pytest.raises(UnsafeQueryError):
        assert_read_only_sql(f"SELECT * FROM {routine}(1)")


def test_allowlists_are_subsets_of_declared_routines():
    assert ALLOWED_PROCEDURES <= KNOWN_PROCEDURES
    assert ALLOWED_FUNCTIONS <= KNOWN_FUNCTIONS
    assert len(ALLOWED_PROCEDURES) == 11
    assert len(ALLOWED_FUNCTIONS - {"RDB$GET_CONTEXT"}) == 30
    assert BLOCKED_PROCEDURES <= KNOWN_PROCEDURES - ALLOWED_PROCEDURES


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT GEN_ID(GEN_OBJID_X, 1) FROM RDB$DATABASE", "GEN_ID"),
        ("SELECT GEN_ID(SOMEGEN, 0) FROM RDB$DATABASE", "GEN_ID"),
        ("SELECT NEXT VALUE FOR SOMEGEN FROM RDB$DATABASE", "NEXT VALUE FOR"),
        ("SELECT next\n value  for g FROM RDB$DATABASE", "NEXT VALUE FOR"),
    ],
)
def test_generators_rejected(sql, reason):
    with pytest.raises(UnsafeQueryError, match=reason):
        assert_read_only_sql(sql)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT Token FROM V_SalePayments WHERE Sale = ?", "TOKEN"),
        ("SELECT Token, CVVR FROM V_SaleCharges WHERE Sale = ?", "CVVR"),
        ("SELECT AuthCode FROM V_SaleCharges WHERE Sale = ?", "AUTHCODE"),
        ("SELECT a.Token FROM V_SaleArmPlan a WHERE a.Sale = ?", "TOKEN"),
        ("SELECT GoogleAuthToken FROM V_ExportConfig", "GOOGLEAUTHTOKEN"),
        ("SELECT EncryptionKey FROM V_ChainSpecific", "ENCRYPTIONKEY"),
        ("SELECT * FROM V_EmployeeSession", "V_EMPLOYEESESSION"),
        ("SELECT * FROM V_ExportConfig", "V_EXPORTCONFIG"),
    ],
)
def test_payment_tokens_and_secret_views_rejected(sql, reason):
    with pytest.raises(UnsafeQueryError, match=reason):
        assert_read_only_sql(sql)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        # Under dialect 1 these are string literals (no bypass); under dialect 3 they would
        # be quoted identifiers, so the guard checks the quoted words too.
        ('SELECT * FROM "GEN_OBJID"', "GEN_OBJID"),
        ('SELECT * FROM "PREPAID_SET_BALANCE"(1, 2)', "PREPAID_SET_BALANCE"),
        ('SELECT e."SSNUM" FROM V_Employee e', "SSNUM"),
        ('SELECT * FROM "V_EMPLOYEE"', "V_EMPLOYEE"),
        ('SELECT "F_IBPASSWORD"() FROM RDB$DATABASE', "F_IBPASSWORD"),
    ],
)
def test_double_quoted_identifiers_checked(sql, reason):
    with pytest.raises(UnsafeQueryError, match=reason):
        assert_read_only_sql(sql)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT COUNT(*) FROM V_Sale", "without a WHERE clause"),
        ("SELECT * FROM CustClubPlan_Balances('1/1/1980','Now')", "many minutes"),
        ("SELECT FIRST 100 * FROM Arm_Monthly_Current_Status(?)", "many minutes"),
    ],
)
def test_load_warnings(sql, expected):
    assert_read_only_sql(sql)
    assert any(expected in w for w in query_load_warnings(sql))


def test_no_load_warning_for_bounded_query():
    sql = "SELECT COUNT(*) FROM V_Sale WHERE Site = ? AND LogDate >= ? AND LogDate < ?"
    assert query_load_warnings(sql) == []
