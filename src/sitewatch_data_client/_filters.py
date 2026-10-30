"""Date and site normalization plus parameterized SQL fragment builders.

Every user value reaches SQL as a ``?`` parameter. The only text these helpers put into SQL
is placeholder strings and library-owned column names.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sitewatch_data_client._constants import (
    BUSINESS_TIME_ZONE,
    ICON_ALL_SITE_IDS,
    ICON_OFFICE_SITE_ID,
    ICON_QUERY_SERVER_SITE_ID,
    ICON_SITE_IDS,
)
from sitewatch_data_client._exceptions import ValidationError

__all__ = [
    "ICON_ALL_SITE_IDS",
    "ICON_OFFICE_SITE_ID",
    "ICON_QUERY_SERVER_SITE_ID",
    "ICON_SITE_IDS",
    "DateInput",
    "Sites",
    "build_in_clause",
    "normalize_date",
    "normalize_date_range",
    "normalize_site",
    "normalize_sites",
    "placeholders",
]

Sites = int | Iterable[int] | None
DateInput = date | datetime | str


def business_today() -> date:
    """Return today's date in US Central time, the time zone of the ICON business dates.

    Used instead of ``date.today()`` so a UTC host (CI, a cloud runner) does not move
    "today" by a day around midnight.
    """
    return datetime.now(ZoneInfo(BUSINESS_TIME_ZONE)).date()


def normalize_date(value: DateInput | None) -> date | None:
    """Normalize a date argument to ``datetime.date`` (or None).

    Args:
        value: A ``date``, a ``datetime`` (the time part is dropped), an ISO ``YYYY-MM-DD``
            string, or None.

    Returns:
        The calendar date, or None when ``value`` is None.

    Raises:
        ValidationError: If the string is not ISO format or the type is unsupported.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            raise ValidationError(
                f"Invalid date format: '{value}'. "
                "Expected ISO format (YYYY-MM-DD), a date or a datetime."
            ) from None
    raise ValidationError(
        f"Invalid date type: expected date, datetime or ISO string, got {type(value).__name__}."
    )


def normalize_date_range(
    start: DateInput, end: DateInput, max_days: int | None = None
) -> tuple[datetime, datetime]:
    """Validate an inclusive date range and return a half-open timestamp range.

    SiteWatch ``LogDate`` and ``PunchDate`` hold the business date at midnight, so the
    returned pair is used as ``LogDate >= ? AND LogDate < ?``.

    Args:
        start: First business date (inclusive).
        end: Last business date (inclusive).
        max_days: Maximum length of the range in days, counting both ends (so 31 allows
            ``2026-10-01`` to ``2026-10-31``). None means no cap.

    Returns:
        ``(start_midnight, day_after_end_midnight)`` as naive datetimes.

    Raises:
        ValidationError: If either date is missing or invalid, ``start > end``, or the range
            is longer than ``max_days``.

    Examples:
        >>> normalize_date_range("2026-10-01", "2026-10-14", max_days=31)
        (datetime.datetime(2026, 10, 1, 0, 0), datetime.datetime(2026, 10, 15, 0, 0))
    """
    s = normalize_date(start)
    e = normalize_date(end)
    if s is None or e is None:
        raise ValidationError("Both start and end dates are required.")
    if s > e:
        raise ValidationError(f"start ({s}) must be on or before end ({e}).")
    days = (e - s).days + 1
    if max_days is not None and days > max_days:
        raise ValidationError(
            f"Date range of {days} days exceeds the {max_days}-day limit for this method. "
            "Split the range into smaller calls, or use client.query() for a custom query."
        )
    start_dt = datetime(s.year, s.month, s.day)
    end_dt = datetime(e.year, e.month, e.day) + timedelta(days=1)
    return start_dt, end_dt


def _validate_site_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"Invalid site ID: expected int, got {type(value).__name__}.")
    if value <= 0:
        raise ValidationError(f"Invalid site ID: {value}. Site IDs are positive integers.")
    return value


def normalize_site(site: int) -> int:
    """Validate a single required site ID.

    Raises:
        ValidationError: If ``site`` is not a positive int (bools are rejected).
    """
    return _validate_site_id(site)


def normalize_sites(sites: Sites) -> list[int]:
    """Normalize a site argument to a sorted, de-duplicated list of site IDs.

    Args:
        sites: A single site ID, an iterable of site IDs, or None for the ICON car-wash
            sites (5, 13, 18, 20, 24, 25).

    Returns:
        Sorted list of unique site IDs.

    Raises:
        ValidationError: If a value is not a positive int (bools are rejected), or the
            iterable is empty.
    """
    if sites is None:
        return list(ICON_SITE_IDS)
    if isinstance(sites, (str, bytes)):
        raise ValidationError("Invalid sites: expected int or iterable of ints, got str.")
    if isinstance(sites, int) and not isinstance(sites, bool):
        return [_validate_site_id(sites)]
    if isinstance(sites, bool):
        raise ValidationError("Invalid site ID: expected int, got bool.")
    try:
        items = list(sites)
    except TypeError:
        raise ValidationError(
            f"Invalid sites: expected int or iterable of ints, got {type(sites).__name__}."
        ) from None
    if not items:
        raise ValidationError("sites cannot be empty. Pass None for the ICON car-wash sites.")
    return sorted({_validate_site_id(v) for v in items})


def placeholders(n: int) -> str:
    """Return ``n`` comma-separated qmark placeholders, e.g. ``"?, ?, ?"``.

    Raises:
        ValidationError: If ``n`` is less than 1.
    """
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValidationError("placeholders() needs a positive count.")
    return ", ".join(["?"] * n)


def build_in_clause(column: str, values: Iterable[object]) -> tuple[str, list[object]]:
    """Build a parameterized ``column IN (?, ...)`` clause.

    Args:
        column: Library-owned column expression (never user input).
        values: Values to bind; each becomes one ``?`` parameter.

    Returns:
        ``(sql_fragment, params)``.

    Examples:
        >>> build_in_clause("s.Site", [5, 24])
        ('s.Site IN (?, ?)', [5, 24])
    """
    params = list(values)
    return f"{column} IN ({placeholders(len(params))})", params
