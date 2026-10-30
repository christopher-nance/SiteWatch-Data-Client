"""Tests for date/site normalization and SQL fragment builders."""

from datetime import date, datetime

import pytest

from sitewatch_data_client._constants import ICON_ALL_SITE_IDS, ICON_SITE_IDS
from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client._filters import (
    build_in_clause,
    normalize_date,
    normalize_date_range,
    normalize_site,
    normalize_sites,
    placeholders,
)


class TestNormalizeDate:
    def test_none(self):
        assert normalize_date(None) is None

    def test_date(self):
        assert normalize_date(date(2026, 10, 14)) == date(2026, 10, 14)

    def test_datetime_drops_time(self):
        assert normalize_date(datetime(2026, 10, 14, 15, 30)) == date(2026, 10, 14)

    def test_iso_string(self):
        assert normalize_date("2026-10-14") == date(2026, 10, 14)
        assert normalize_date(" 2026-10-14 ") == date(2026, 10, 14)

    def test_invalid_string(self):
        with pytest.raises(ValidationError, match="Invalid date format"):
            normalize_date("10/14/2026")

    def test_invalid_type(self):
        with pytest.raises(ValidationError, match="Invalid date type"):
            normalize_date(20261014)  # type: ignore[arg-type]


class TestNormalizeDateRange:
    def test_half_open(self):
        start, end = normalize_date_range("2026-10-01", "2026-10-14")
        assert start == datetime(2026, 10, 1)
        assert end == datetime(2026, 10, 15)

    def test_single_day(self):
        start, end = normalize_date_range(date(2026, 10, 14), date(2026, 10, 14), max_days=1)
        assert (end - start).days == 1

    def test_month_rollover(self):
        _, end = normalize_date_range("2026-10-31", "2026-10-31")
        assert end == datetime(2026, 11, 1)

    def test_start_after_end(self):
        with pytest.raises(ValidationError, match="on or before"):
            normalize_date_range("2026-10-14", "2026-10-01")

    def test_limit_inclusive(self):
        normalize_date_range("2026-10-01", "2026-10-31", max_days=31)
        with pytest.raises(ValidationError, match="31-day limit"):
            normalize_date_range("2026-10-01", "2026-11-01", max_days=31)

    def test_missing(self):
        with pytest.raises(ValidationError, match="required"):
            normalize_date_range(None, "2026-10-01")  # type: ignore[arg-type]


class TestNormalizeSites:
    def test_none_defaults_to_icon_car_washes(self):
        assert normalize_sites(None) == [5, 13, 18, 20, 24, 25]
        assert tuple(normalize_sites(None)) == ICON_SITE_IDS

    def test_constants(self):
        assert 99 in ICON_ALL_SITE_IDS and 100 in ICON_ALL_SITE_IDS
        assert 99 not in ICON_SITE_IDS

    def test_single_int(self):
        assert normalize_sites(24) == [24]

    def test_list_sorted_dedup(self):
        assert normalize_sites([24, 5, 24, 13]) == [5, 13, 24]

    def test_tuple_and_generator(self):
        assert normalize_sites((25, 20)) == [20, 25]
        assert normalize_sites(x for x in [18]) == [18]

    @pytest.mark.parametrize("bad", [True, 0, -1, "24", 2.5, [24, "5"], [False], [0]])
    def test_invalid(self, bad):
        with pytest.raises(ValidationError):
            normalize_sites(bad)  # type: ignore[arg-type]

    def test_empty_list(self):
        with pytest.raises(ValidationError, match="empty"):
            normalize_sites([])

    def test_normalize_site(self):
        assert normalize_site(24) == 24
        with pytest.raises(ValidationError):
            normalize_site(True)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            normalize_site(None)  # type: ignore[arg-type]


class TestPlaceholders:
    def test_placeholders(self):
        assert placeholders(1) == "?"
        assert placeholders(3) == "?, ?, ?"

    @pytest.mark.parametrize("bad", [0, -1, True])
    def test_invalid(self, bad):
        with pytest.raises(ValidationError):
            placeholders(bad)

    def test_build_in_clause(self):
        sql, params = build_in_clause("s.Site", [5, 24])
        assert sql == "s.Site IN (?, ?)"
        assert params == [5, 24]

    def test_build_in_clause_never_interpolates_values(self):
        sql, params = build_in_clause("s.Site", ["1); DROP TABLE x; --"])
        assert "DROP" not in sql
        assert params == ["1); DROP TABLE x; --"]
