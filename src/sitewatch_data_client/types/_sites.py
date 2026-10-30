"""Pydantic result models for site master data and data freshness."""

from __future__ import annotations

from datetime import date, datetime

from sitewatch_data_client.types._base import SiteWatchModel


class Site(SiteWatchModel):
    """A SiteWatch site: identity from ``V_Site`` plus address from ``V_System``.

    ``V_System.Name`` is always ``"System"`` and is never used as the name.
    """

    site_id: int
    """``V_Site.ID`` (for example 24). Not the same number as the code suffix."""
    code: str
    """``V_Site.Code``, for example ``ICONTN-030``."""
    name: str
    """``V_Site.SiteName``, trimmed."""
    enabled: bool
    """True when ``V_Site.Enabled == 1`` (1 Enabled, 2 Disabled)."""
    address: str | None = None
    address2: str | None = None
    city: str | None = None
    """City exactly as typed in ``V_System`` (casing and spelling are not normalized)."""
    state: str | None = None
    zip: str | None = None
    phone: str | None = None


class SiteFreshness(SiteWatchModel):
    """How current the central database is for one site.

    The central database lags the sites, so check this before trusting a recent window.
    """

    site_id: int
    site_code: str
    latest_sale_date: date | None = None
    """Newest ``V_Sale.LogDate`` within the last 400 days, or None if there is none."""
    latest_punch_date: date | None = None
    """Newest ``V_PunchInterval.PunchDate`` within the last 400 days, or None."""
    replication_applied_at: datetime | None = None
    """``V_Replication.PacketAppliedDate`` (site-local wall clock), or None if no row."""
