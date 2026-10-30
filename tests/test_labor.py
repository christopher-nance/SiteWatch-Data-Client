"""Tests for LaborResource and the timecard logic, with a mocked Firebird connection.

All names and IDs are made up ("Pat Example", employee number 99999).
"""

import logging
from datetime import date, datetime

import pytest

from sitewatch_data_client import ValidationError
from sitewatch_data_client._constants import ICON_SITE_IDS
from sitewatch_data_client.resources._labor import (
    LaborResource,
    aggregate_daily_hours,
    build_intervals,
    build_totals,
    classify_actual,
    same_minute,
    us_dst_change_dates,
    week_start,
)
from sitewatch_data_client.types._labor import (
    Employee,
    LaborStat,
    PunchSource,
    TimecardFlag,
    TimecardInterval,
    TimecardReport,
)
from tests.conftest import make_df

SENSITIVE = (
    "SSNUM",
    "PWORD",
    "SECURITYCODE",
    "DRVLNUM",
    "WEBPASSWORD",
    "ADDRESS",
    "PHONE",
    "BIRTH",
    "WAGE",
    "RATE",
)


@pytest.fixture
def labor(mock_client):
    return LaborResource(mock_client)


def sent(mock_client):
    """Return (sql, params) from the last query sent to the mocked connection."""
    args = mock_client._conn.query.call_args
    return args[0][0], list(args[0][1])


def assert_no_sensitive(sql):
    upper = sql.upper()
    for name in SENSITIVE:
        assert name not in upper, name


def punch_row(**kw):
    row = {
        "site": 24,
        "sitecode": "ICONTN-030",
        "sitename": "Example Site",
        "employeeid": 900001,
        "employeenumber": 99999,
        "firstname": "Pat",
        "lastname": "Example",
        "fullname": "Pat Example",
        "homesite": 24,
        "empcategory": "Line Labor",
        "laborroleid": 102,
        "laborrole": "Line Labor",
        "pitype": "R",
        "punchdate": datetime(2026, 10, 5),
        "intime": 480,
        "outtime": 720,
        "actualin": 4803,
        "actualout": 7201,
        "multiplier": 1.0,
    }
    row.update(kw)
    return row


def punch_df(rows):
    keys = rows[0].keys()
    return make_df({k: [r[k] for r in rows] for k in keys})


# --- Pure timecard logic ----------------------------------------------------------------------


class TestSameMinute:
    @pytest.mark.parametrize(
        ("tenths", "minute", "expected"),
        [
            (6489, 648, True),  # 10:48:54 -> 10:48
            (6480, 648, True),
            (3510, 350, True),  # rounding exception: ends in 0, one minute above
            (3511, 350, False),  # not ending in 0: a real mismatch
            (3520, 350, False),  # two minutes above
            (9814, 758, False),  # time of day the edit was saved
            (0, 0, True),
        ],
    )
    def test_cases(self, tenths, minute, expected):
        assert same_minute(tenths, minute) is expected


class TestClassifyActual:
    @pytest.mark.parametrize(
        ("actual", "minute", "expected"),
        [
            (None, 480, PunchSource.UNKNOWN),
            (30000, 720, PunchSource.MISSING_OUT),
            (30003, 720, PunchSource.MISSING_OUT),
            (30004, 720, PunchSource.MISSING_OUT),
            (30001, 480, PunchSource.EDITED),
            (30002, 480, PunchSource.EDITED),
            (-30000, 0, PunchSource.CONTINUATION),
            (-30001, 600, PunchSource.CONTINUATION),
            (-30002, 600, PunchSource.CONTINUATION),
            (-4805, 480, PunchSource.GROUP),
            (-5005, 480, PunchSource.EDITED),
            (4805, 480, PunchSource.TERMINAL),
            (4810, 480, PunchSource.TERMINAL),  # rounding exception
            (9000, 480, PunchSource.EDITED),
            (28801, 480, PunchSource.OTHER),
            (-29000, 480, PunchSource.OTHER),
        ],
    )
    def test_codes(self, actual, minute, expected):
        assert classify_actual(actual, minute) is expected


class TestWeekStart:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 10, 3), date(2026, 10, 3)),  # Saturday
            (date(2026, 10, 9), date(2026, 10, 3)),  # Friday
            (date(2026, 10, 10), date(2026, 10, 10)),
            (date(2026, 10, 4), date(2026, 10, 3)),  # Sunday
        ],
    )
    def test_saturday_start(self, day, expected):
        assert week_start(day) == expected

    def test_dst_dates(self):
        assert us_dst_change_dates(2026) == (date(2026, 3, 8), date(2026, 11, 1))
        assert us_dst_change_dates(2027) == (date(2027, 3, 14), date(2027, 11, 7))


class TestBuildIntervals:
    def test_terminal_interval(self):
        [i] = build_intervals([punch_row()])
        assert i.clock_in == datetime(2026, 10, 5, 8, 0)
        assert i.clock_out == datetime(2026, 10, 5, 12, 0)
        assert i.hours == 4.0
        assert i.pay_week_start == date(2026, 10, 3)
        assert i.in_source is PunchSource.TERMINAL
        assert i.out_source is PunchSource.TERMINAL
        assert i.flags == []
        assert i.new_shift is True
        assert i.gap_before_min is None
        assert i.labor_role_id == 102

    def test_hours_four_dp(self):
        [i] = build_intervals([punch_row(intime=480, outtime=487, actualout=4870)])
        assert i.hours == 0.1167

    def test_open_and_edited(self):
        [i] = build_intervals([punch_row(actualin=30002, actualout=30000)])
        assert i.flags == [TimecardFlag.OPEN, TimecardFlag.EDITED, TimecardFlag.OPEN_CHAIN]
        assert i.out_source is PunchSource.MISSING_OUT

    def test_rounding_exception_not_edited(self):
        [i] = build_intervals([punch_row(actualin=4810)])
        assert TimecardFlag.EDITED not in i.flags

    def test_crosses_midnight_and_continuation(self):
        rows = [
            punch_row(intime=1320, outtime=1440, actualin=13203, actualout=-30000),
            punch_row(
                punchdate=datetime(2026, 10, 6),
                intime=0,
                outtime=60,
                actualin=-30000,
                actualout=601,
            ),
        ]
        a, b = build_intervals(rows)
        assert TimecardFlag.CROSSES_MIDNIGHT in a.flags
        assert a.clock_out == datetime(2026, 10, 6, 0, 0)
        assert b.in_source is PunchSource.CONTINUATION
        assert b.new_shift is False
        assert b.gap_before_min is None

    def test_overtime_flag(self):
        [i] = build_intervals([punch_row(pitype="O", multiplier=1.5)])
        assert i.flags == [TimecardFlag.SW_OVERTIME]
        assert i.multiplier == 1.5

    def test_lunch_gap_same_shift(self):
        rows = [
            punch_row(intime=480, outtime=720, actualin=4801, actualout=7201),
            punch_row(intime=750, outtime=960, actualin=7501, actualout=9601),
        ]
        a, b = build_intervals(rows)
        assert a.new_shift is True
        assert b.new_shift is False
        assert b.gap_before_min == 30
        assert TimecardFlag.OVERLAP not in b.flags

    def test_overlap_across_sites(self):
        rows = [
            punch_row(site=5, sitecode="ICONTN-005", intime=480, outtime=720),
            punch_row(site=24, intime=600, outtime=800, actualin=6001, actualout=8001),
        ]
        a, b = build_intervals(rows)
        assert TimecardFlag.OVERLAP in b.flags
        assert b.new_shift is True  # different site, different shift

    def test_nan_optional_fields(self):
        rows = [punch_row(), punch_row(employeeid=900002, actualin=4801, intime=480)]
        df = punch_df(rows)
        df.loc[1, "employeenumber"] = None
        df.loc[1, "firstname"] = None
        df.loc[1, "laborrole"] = "  "
        df.loc[1, "multiplier"] = None
        _, b = build_intervals(df.to_dict("records"))
        assert b.employee_number is None
        assert b.first_name is None
        assert b.labor_role is None
        assert b.multiplier is None


class TestBuildTotals:
    def test_aggregates_per_site_with_weekly_all_sites(self):
        rows = [
            punch_row(site=24, intime=480, outtime=720),
            punch_row(site=24, intime=750, outtime=960, actualin=7501, actualout=30000),
            punch_row(site=24, pitype="O", intime=960, outtime=1020, actualin=-30002),
            punch_row(
                site=5,
                sitecode="ICONTN-005",
                punchdate=datetime(2026, 10, 6),
                actualin=30001,
            ),
        ]
        totals = build_totals(build_intervals(rows))
        assert [t.site_id for t in totals] == [5, 24]
        site5, site24 = totals
        assert site24.total_hours == 8.5
        assert site24.sw_regular_hours == 7.5
        assert site24.sw_overtime_hours == 1.0
        assert site24.shifts == 1
        assert site24.intervals == 3
        assert site24.open_punches == 1
        assert site24.open_hours == 3.5
        assert site5.edited_punches == 1
        assert [(w.pay_week_start, w.hours) for w in site24.weekly_hours_all_sites] == [
            (date(2026, 10, 3), 12.5)
        ]


# --- timecards -------------------------------------------------------------------------------


class TestTimecards:
    def test_sql_parameterized_with_default_sites(self, labor, mock_client):
        mock_client._conn.query.return_value = punch_df([punch_row()])
        result = labor.timecards("2026-10-03", "2026-10-09")
        sql, params = sent(mock_client)
        assert len(result) == 1
        assert isinstance(result[0], TimecardInterval)
        # One look-ahead day (10-10) to match -24:00 pieces with an open continuation.
        assert params == [
            *ICON_SITE_IDS,
            datetime(2026, 10, 3),
            datetime(2026, 10, 11),
            "R",
            "O",
        ]
        assert "p.Site IN (?, ?, ?, ?, ?, ?)" in sql
        assert "p.PunchDate >= ? AND p.PunchDate < ?" in sql
        assert "p.PiType IN (?, ?)" in sql
        assert "2026" not in sql
        assert sql.rstrip().endswith(
            "ORDER BY e.LastName, e.FirstName, p.Employee, p.PunchDate, p.InTime, p.Site"
        )
        assert_no_sensitive(sql)

    def test_single_site_and_pi_types(self, labor, mock_client):
        labor.timecards(date(2026, 10, 3), date(2026, 10, 3), 24, pi_types=["r", "A"])
        sql, params = sent(mock_client)
        assert params == [24, datetime(2026, 10, 3), datetime(2026, 10, 5), "R", "A"]

    def test_empty(self, labor, mock_client):
        assert labor.timecards("2026-10-03", "2026-10-09") == []

    def test_range_limit(self, labor, mock_client):
        with pytest.raises(ValidationError, match="31-day"):
            labor.timecards("2026-09-01", "2026-10-02")
        mock_client._conn.query.assert_not_called()

    @pytest.mark.parametrize("bad", [[], ["RO"], [1], ["1"], ["R'; --"]])
    def test_bad_pi_types(self, labor, mock_client, bad):
        with pytest.raises(ValidationError):
            labor.timecards("2026-10-03", "2026-10-09", pi_types=bad)
        mock_client._conn.query.assert_not_called()

    @pytest.mark.parametrize("bad", [[], [0], [True], ["24"], "24"])
    def test_bad_sites(self, labor, mock_client, bad):
        with pytest.raises(ValidationError):
            labor.timecards("2026-10-03", "2026-10-09", bad)

    def test_inverted_range(self, labor):
        with pytest.raises(ValidationError):
            labor.timecards("2026-10-09", "2026-10-03")


class TestTimecardTotals:
    def test_report_one_query(self, labor, mock_client):
        mock_client._conn.query.return_value = punch_df(
            [punch_row(punchdate=datetime(2026, 10, 9))]
        )
        report = labor.timecard_totals("2026-10-03", "2026-10-09", 24)
        assert isinstance(report, TimecardReport)
        assert mock_client._conn.query.call_count == 1
        assert report.whole_pay_weeks is True
        assert report.latest_punch_date == date(2026, 10, 9)
        assert len(report.intervals) == 1
        assert report.totals[0].total_hours == 4.0
        assert report.warnings == []

    def test_warnings(self, labor, mock_client, caplog):
        mock_client._conn.query.return_value = punch_df(
            [
                punch_row(actualout=30000),
                punch_row(site=5, sitecode="ICONTN-005", intime=600, outtime=700),
            ]
        )
        with caplog.at_level(logging.WARNING):
            report = labor.timecard_totals("2026-10-04", "2026-10-09", [5, 24])
        text = " | ".join(report.warnings)
        assert "Latest punch found is 2026-10-05" in text
        assert "1 OPEN" in text
        assert "1 OVERLAP" in text
        assert "not whole Sat-Fri pay weeks" in text
        assert report.whole_pay_weeks is False
        assert len(caplog.records) == len(report.warnings)
        assert "Pat" not in text and "Example" not in text

    def test_empty_window_warns(self, labor):
        report = labor.timecard_totals("2026-10-03", "2026-10-09")
        assert report.intervals == []
        assert report.totals == []
        assert report.latest_punch_date is None
        assert any("none" in w for w in report.warnings)

    def test_dst_warning_only_on_change_date(self, labor, mock_client):
        mock_client._conn.query.return_value = punch_df(
            [
                punch_row(
                    punchdate=datetime(2026, 10, 31),
                    intime=60,
                    outtime=180,
                    actualin=601,
                    actualout=1801,
                ),
                punch_row(
                    punchdate=datetime(2026, 11, 1),
                    intime=60,
                    outtime=180,
                    actualin=601,
                    actualout=1801,
                ),
                punch_row(
                    punchdate=datetime(2026, 11, 1),
                    intime=600,
                    outtime=700,
                    actualin=6001,
                    actualout=7001,
                ),
            ]
        )
        report = labor.timecard_totals("2026-10-31", "2026-11-01", 24)
        dst = [w for w in report.warnings if w.startswith("DST")]
        assert len(dst) == 1
        assert "2026-11-01 01:00" in dst[0]

    def test_range_limit(self, labor):
        with pytest.raises(ValidationError):
            labor.timecard_totals("2026-09-01", "2026-10-09")


# --- employees ---------------------------------------------------------------------------------


def employee_df():
    df = make_df(
        {
            "objid": [900001, 900002],
            "code": [99999, None],
            "firstname": ["Pat", "Sam"],
            "lastname": ["Example", "Sample  "],
            "name": ["Pat Example", ""],
            "homesite": [24, 17],
            "category": [4, None],
            "categoryname": ["Line Labor", None],
            "hiredate": [datetime(2024, 5, 1), None],
            "termdate": [None, datetime(2026, 1, 31)],
            "enabled": [1, 2],
        }
    )
    df["termdate"] = df["termdate"].astype("datetime64[ns]")
    return df


class TestEmployees:
    def test_mapping_and_default_sites(self, labor, mock_client):
        mock_client._conn.query.return_value = employee_df()
        result = labor.employees()
        sql, params = sent(mock_client)
        assert all(isinstance(e, Employee) for e in result)
        a, b = result
        assert a.employee_id == 900001
        assert a.employee_number == 99999
        assert a.hire_date == date(2024, 5, 1)
        assert a.term_date is None
        assert a.enabled is True
        assert b.employee_number is None
        assert b.last_name == "Sample"
        assert b.full_name is None
        assert b.category_id is None
        assert b.term_date == date(2026, 1, 31)
        assert b.enabled is False
        assert params[:6] == list(ICON_SITE_IDS)
        assert params[6] == date.today()
        assert "e.HomeSite IN (?, ?, ?, ?, ?, ?)" in sql
        assert "e.Enabled = 1 AND (e.TermDate IS NULL OR e.TermDate > ?)" in sql
        assert "TODAY" not in sql.upper()
        assert_no_sensitive(sql)
        assert "*" not in sql

    def test_all_records(self, labor, mock_client):
        labor.employees(24, active_only=False)
        sql, params = sent(mock_client)
        assert params == [24]
        assert "Enabled" not in sql.split("WHERE")[1]

    def test_employee_ids_without_sites(self, labor, mock_client):
        labor.employees(employee_ids=[900002, 900001, 900001], active_only=False)
        sql, params = sent(mock_client)
        assert params == [900001, 900002]
        assert "e.ObjID IN (?, ?)" in sql
        assert "HomeSite" not in sql.split("WHERE")[1]

    def test_employee_ids_with_sites(self, labor, mock_client):
        labor.employees(24, employee_ids=[900001])
        _, params = sent(mock_client)
        assert params == [900001, 24, date.today()]

    @pytest.mark.parametrize("bad", [[], ["900001"], [True], [1.5]])
    def test_bad_employee_ids(self, labor, mock_client, bad):
        with pytest.raises(ValidationError):
            labor.employees(employee_ids=bad)
        mock_client._conn.query.assert_not_called()

    def test_too_many_ids(self, labor):
        with pytest.raises(ValidationError, match="500"):
            labor.employees(employee_ids=range(1, 502))

    def test_empty(self, labor):
        assert labor.employees(24) == []


class TestWorkedEmployees:
    def test_sql_and_mapping(self, labor, mock_client):
        mock_client._conn.query.return_value = employee_df()
        result = labor.worked_employees("2026-10-01", "2026-10-14", [24, 5])
        sql, params = sent(mock_client)
        assert len(result) == 2
        assert params == [5, 24, datetime(2026, 10, 1), datetime(2026, 10, 15)]
        assert "IN (SELECT" not in sql.upper()
        assert "GROUP BY p.Employee) w" in sql
        assert_no_sensitive(sql)

    def test_range_limit(self, labor):
        labor.worked_employees("2026-08-01", "2026-11-01")  # 93 days ok
        with pytest.raises(ValidationError):
            labor.worked_employees("2026-08-01", "2026-11-02")


# --- labor_statistics --------------------------------------------------------------------------


def stats_df(dollars=False):
    data = {
        "punchdate": [None, None, None],
        "employee": [900002, 900001, 900001],
        "employeenumber": [99998, 99999, 99999],
        "firstname": ["Sam", "Pat", "Pat"],
        "lastname": ["Sample", "Example", "Example"],
        "role": [None, None, None],
        "rolename": [None, None, None],
        "pitype": ["R", "R", "O"],
        "minutes": [600, 2425, 125],
    }
    if dollars:
        data["dollars"] = [100.10000000000001, 404.17, 31.25]
    return make_df(data)


class TestLaborStatistics:
    def test_default_employee_grouping(self, labor, mock_client):
        mock_client._conn.query.return_value = stats_df()
        result = labor.labor_statistics("2026-10-03", "2026-10-09", 24)
        sql, params = sent(mock_client)
        assert params == [
            datetime(2026, 10, 3),
            datetime(2026, 10, 9),  # ThruDate is inclusive
            24,
            None,
            1,
            None,
            None,
            None,
            None,
            None,
        ]
        assert "Labor_Statistics(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" in sql
        assert "Dollars" not in sql
        assert_no_sensitive(sql)
        assert all(isinstance(s, LaborStat) for s in result)
        # sorted by name: Example (O, R) then Sample
        assert [(s.last_name, s.pi_type) for s in result] == [
            ("Example", "O"),
            ("Example", "R"),
            ("Sample", "R"),
        ]
        r = result[1]
        assert r.site_id == 24
        assert r.minutes == 2425.0
        assert r.hours == 40.42
        assert r.dollars is None
        assert r.role_id is None
        assert r.punch_date is None

    def test_group_by_order_and_filters(self, labor, mock_client):
        labor.labor_statistics(
            "2026-10-03",
            "2026-10-09",
            24,
            group_by=("role", "date"),
            profit_center=1,
            labor_role=102,
            employee_category=4,
            employee_id=900001,
        )
        _, params = sent(mock_client)
        assert params[3:] == [2, None, 1, 1, 102, 4, 900001]

    def test_group_by_string(self, labor, mock_client):
        labor.labor_statistics("2026-10-03", "2026-10-09", 24, group_by="date")
        _, params = sent(mock_client)
        assert params[3:6] == [1, None, None]

    def test_include_dollars_rounds(self, labor, mock_client):
        mock_client._conn.query.return_value = stats_df(dollars=True)
        result = labor.labor_statistics("2026-10-03", "2026-10-09", 24, include_dollars=True)
        sql, _ = sent(mock_client)
        assert "CAST(ls.Dollars AS NUMERIC(12,2)) AS Dollars" in sql
        assert {s.dollars for s in result} == {100.1, 404.17, 31.25}

    @pytest.mark.parametrize("bad", [(), [], ("week",), ("date", "date")])
    def test_bad_group_by(self, labor, mock_client, bad):
        with pytest.raises(ValidationError):
            labor.labor_statistics("2026-10-03", "2026-10-09", 24, group_by=bad)
        mock_client._conn.query.assert_not_called()

    def test_bad_filter(self, labor):
        with pytest.raises(ValidationError):
            labor.labor_statistics("2026-10-03", "2026-10-09", 24, employee_id="900001")

    @pytest.mark.parametrize("bad", [None, 0, True, "24", [24]])
    def test_site_required(self, labor, bad):
        with pytest.raises(ValidationError):
            labor.labor_statistics("2026-10-03", "2026-10-09", bad)

    def test_range_limit(self, labor):
        with pytest.raises(ValidationError):
            labor.labor_statistics("2026-09-01", "2026-10-09", 24)

    def test_empty(self, labor):
        assert labor.labor_statistics("2026-10-03", "2026-10-09", 24) == []


# --- daily_hours ----------------------------------------------------------------------------------


def chain_rows(home_site=99, employee=900002, site=24):
    """A made-up open shift: 10:36-24:00 on 10-01 continued as 00:00-02:36 OPEN on 10-02."""
    return [
        punch_row(
            site=site,
            employeeid=employee,
            homesite=home_site,
            punchdate=datetime(2026, 10, 1),
            intime=636,
            outtime=1440,
            actualin=6361,
            actualout=-30000,
        ),
        punch_row(
            site=site,
            employeeid=employee,
            homesite=home_site,
            punchdate=datetime(2026, 10, 2),
            intime=0,
            outtime=156,
            actualin=-30000,
            actualout=30000,
        ),
    ]


class TestOpenChains:
    def test_midnight_piece_before_open_continuation_is_flagged(self):
        first, second = build_intervals(chain_rows())
        assert TimecardFlag.OPEN not in first.flags
        assert TimecardFlag.CROSSES_MIDNIGHT in first.flags
        assert TimecardFlag.OPEN_CHAIN in first.flags
        assert second.flags[:1] == [TimecardFlag.OPEN]
        assert TimecardFlag.OPEN_CHAIN in second.flags

    def test_overtime_split_chain(self):
        rows = [
            punch_row(intime=576, outtime=1056, actualin=5761, actualout=-30002),
            punch_row(intime=1056, outtime=1440, actualin=-30002, actualout=30004, pitype="O"),
        ]
        a, b = build_intervals(rows)
        assert TimecardFlag.OPEN_CHAIN in a.flags and TimecardFlag.OPEN not in a.flags
        assert TimecardFlag.OPEN in b.flags

    def test_closed_midnight_shift_not_flagged(self):
        rows = chain_rows()
        rows[1]["actualout"] = 1561
        first, second = build_intervals(rows)
        assert TimecardFlag.OPEN_CHAIN not in first.flags
        assert TimecardFlag.OPEN_CHAIN not in second.flags

    def test_other_employee_not_linked(self):
        rows = chain_rows()
        rows[1]["employeeid"] = 900003
        first, _ = build_intervals(rows)
        assert TimecardFlag.OPEN_CHAIN not in first.flags

    def test_totals_count_whole_open_shift(self):
        [t] = build_totals(build_intervals(chain_rows(home_site=24)))
        assert t.open_punches == 1
        assert t.open_hours == round(13.4 + 2.6, 2)

    def test_timecards_use_lookahead_day_and_drop_it(self, labor, mock_client):
        mock_client._conn.query.return_value = punch_df(chain_rows())
        cards = labor.timecards("2026-10-01", "2026-10-01", 24)
        _, params = sent(mock_client)
        assert params[1:3] == [datetime(2026, 10, 1), datetime(2026, 10, 3)]
        assert [c.punch_date for c in cards] == [date(2026, 10, 1)]
        assert TimecardFlag.OPEN_CHAIN in cards[0].flags

    def test_open_warning_reports_chain_hours(self, labor, mock_client):
        mock_client._conn.query.return_value = punch_df(chain_rows(home_site=24))
        report = labor.timecard_totals("2026-10-01", "2026-10-02", 24)
        assert any("16.00 hours" in w for w in report.warnings)


class TestDailyHours:
    def _rows(self):
        return [
            punch_row(site=24, employeeid=1, intime=480, outtime=1000, pitype="R"),
            punch_row(site=24, employeeid=1, intime=1000, outtime=1061, pitype="O"),
            punch_row(site=24, employeeid=2, intime=480, outtime=960),
            punch_row(site=5, sitecode="ICONTN-005", employeeid=3, homesite=5),
            *chain_rows(home_site=99, employee=4, site=24),
            *chain_rows(home_site=24, employee=5, site=24),
        ]

    def test_sql_has_lookahead_and_no_sensitive_columns(self, labor, mock_client):
        labor.daily_hours("2026-10-01", "2026-10-01")
        sql, params = sent(mock_client)
        assert params == [*ICON_SITE_IDS, datetime(2026, 10, 1), datetime(2026, 10, 3)]
        assert "LEFT JOIN V_Employee e ON e.ObjID = p.Employee" in sql
        assert "JOIN V_Site s ON s.ID = p.Site" in sql
        assert sql.rstrip().endswith("ORDER BY p.Employee, p.PunchDate, p.InTime, p.Site")
        assert_no_sensitive(sql)

    def test_office_accounts_excluded_and_open_chain_marks_day(self, labor, mock_client):
        rows = self._rows()
        for r in rows[:4]:
            r["punchdate"] = datetime(2026, 10, 1)
        mock_client._conn.query.return_value = punch_df(rows)
        result = labor.daily_hours("2026-10-01", "2026-10-01")
        assert [(d.site_id, d.punch_date) for d in result] == [
            (5, date(2026, 10, 1)),
            (24, date(2026, 10, 1)),
        ]
        d = result[1]
        # employees 1, 2 and 5 (employee 4 is an office account, home site 99)
        assert d.employees == 3
        assert d.regular_hours == round((520 + 480) / 60 + 13.4, 2)
        assert d.overtime_hours == 1.02
        assert d.excluded_hours == 13.4
        # employee 5's 10:36-24:00 piece continues as OPEN on the look-ahead day
        assert d.open_punches == 1
        assert d.open_hours == 13.4
        assert d.provisional is True
        assert result[0].provisional is False

    def test_exclusion_can_be_disabled(self, labor, mock_client):
        mock_client._conn.query.return_value = punch_df(chain_rows(home_site=99))
        [d] = labor.daily_hours("2026-10-01", "2026-10-01", 24, exclude_home_sites=None)
        assert d.total_hours == 13.4
        assert d.excluded_hours == 0.0
        assert d.open_punches == 1
        [d] = labor.daily_hours("2026-10-01", "2026-10-01", 24)
        assert d.total_hours == 0.0
        assert d.employees == 0
        assert d.excluded_hours == 13.4
        assert d.provisional is False

    def test_bad_exclude_home_sites(self, labor, mock_client):
        with pytest.raises(ValidationError):
            labor.daily_hours("2026-10-01", "2026-10-01", exclude_home_sites=["99"])

    def test_aggregate_is_pure(self):
        days = aggregate_daily_hours(
            build_intervals(chain_rows(home_site=24)), date(2026, 10, 2), date(2026, 10, 2)
        )
        assert [(d.punch_date, d.total_hours, d.open_hours) for d in days] == [
            (date(2026, 10, 2), 2.6, 2.6)
        ]

    def test_range_limit(self, labor):
        with pytest.raises(ValidationError):
            labor.daily_hours("2026-07-01", "2026-10-14")
