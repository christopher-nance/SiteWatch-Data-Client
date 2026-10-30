"""Result models shared by several resources."""

from __future__ import annotations

from sitewatch_data_client.types._base import SiteWatchModel

__all__ = ["SiteMetric"]


class SiteMetric(SiteWatchModel):
    """One numeric value for one site inside a period total.

    Used by ``client.sales.totals`` and ``client.memberships.activity_totals``.

    Attributes:
        site_id: SiteWatch site ID.
        site_code: ``V_Site.Code``, e.g. ``ICONTN-030``. Not the site ID.
        value: The metric value for the period.
    """

    site_id: int
    site_code: str
    value: float
