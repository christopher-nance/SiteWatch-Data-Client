"""Exception hierarchy for sitewatch-data-client."""


class SiteWatchError(Exception):
    """Base exception for all sitewatch-data-client errors."""


class ConfigurationError(SiteWatchError):
    """Missing or invalid SiteWatch configuration."""


class ConnectionError(SiteWatchError):
    """Firebird connection failure, or a query attempted while not connected."""


class QueryError(SiteWatchError):
    """SQL query execution failure."""


class ValidationError(SiteWatchError):
    """Invalid user input (dates, date ranges, site IDs, arguments)."""


class UnsafeQueryError(ValidationError):
    """SQL rejected by the read-only guard on ``SiteWatchClient.query()``."""
