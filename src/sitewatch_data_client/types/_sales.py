"""Result models for ``client.sales`` (SalesResource)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from sitewatch_data_client.types._base import SiteWatchModel
from sitewatch_data_client.types._common import SiteMetric

__all__ = [
    "DailySales",
    "HourlySales",
    "ItemSales",
    "Sale",
    "SaleLine",
    "SalesTotals",
    "StatusCount",
    "TenderTotal",
]


class DailySales(SiteWatchModel):
    """One site's sales for one business day (``LogDate``).

    Money fields are rounded to 2 dp. Line-type totals (``deposits``, ``paidouts``,
    ``discounts``, ``tendered``) cover **completed** sales only.

    Attributes:
        site_id: SiteWatch site ID (``V_Sale.Site``).
        site_code: Site code from ``V_Site`` (e.g. ``ICONTN-030``). Not the site ID.
        log_date: Business (paid) date.
        completed_sales: Completed sale headers (``f_IsCompleted = 1``). Includes ARM
            recharges, XPT cash events and $0 member washes.
        car_count: SiteWatch's car count: the ``TotalCarCount`` sale fact in profit
            center 1. Includes paid sales whose wash is still pending (``I:P-W``).
        sales_total: ``SUM(V_Sale.Total)`` of completed sales, SiteWatch's raw figure.
        deposits: Base type 170 line total (XPT cash-acceptor deposits, normally <= 0).
        revenue: ``sales_total - deposits``. Net of paid-outs (ARM refunds).
        paidouts: Base type 160 line total (ARM refunds, <= 0). Already inside revenue.
        discounts: Base type 150 line total (ARM redemptions, LPM/employee discounts, <= 0).
        tendered: ``-SUM(Amt)`` of base type 180 lines. Equals ``sales_total`` on balanced
            days.
        pending_wash_sales: ``I:P-W`` sales (paid, wash pending). Counted in ``car_count``,
            not in ``completed_sales``.
        pending_wash_total: ``SUM(V_Sale.Total)`` of those sales. Not in ``sales_total`` or
            ``revenue``.
    """

    site_id: int
    site_code: str
    log_date: date
    completed_sales: int
    car_count: int
    sales_total: float
    deposits: float
    revenue: float
    paidouts: float
    discounts: float
    tendered: float
    pending_wash_sales: int
    pending_wash_total: float


class SalesTotals(SiteWatchModel):
    """Period totals summed from :class:`DailySales`.

    Attributes:
        period_start: First business date (inclusive).
        period_end: Last business date (inclusive).
        completed_sales: Completed sale headers.
        car_count: ``TotalCarCount`` fact total.
        sales_total: ``SUM(V_Sale.Total)`` of completed sales.
        revenue: ``sales_total - deposits``.
        paidouts: Base type 160 line total (inside revenue).
        pending_wash_sales: ``I:P-W`` sales.
        car_count_by_site: Car count per requested site.
        revenue_by_site: Revenue per requested site.
    """

    period_start: date
    period_end: date
    completed_sales: int
    car_count: int
    sales_total: float
    revenue: float
    paidouts: float
    pending_wash_sales: int
    car_count_by_site: list[SiteMetric] = Field(default_factory=list)
    revenue_by_site: list[SiteMetric] = Field(default_factory=list)


class HourlySales(SiteWatchModel):
    """Completed sales and car count for one paid hour.

    Attributes:
        site_id: SiteWatch site ID.
        site_code: Site code from ``V_Site``.
        log_date: Business (paid) date.
        hour: Paid hour 0-23 (``V_SaleStats.Hr``).
        completed_sales: Completed sale headers paid in that hour.
        car_count: ``TotalCarCount`` fact total for sales paid in that hour.
        sales_total: ``SUM(V_Sale.Total)`` of completed sales paid in that hour.
    """

    site_id: int
    site_code: str
    log_date: date
    hour: int
    completed_sales: int
    car_count: int
    sales_total: float


class ItemSales(SiteWatchModel):
    """Line-item totals for completed sales, by item, category or base type.

    Attributes:
        site_id: SiteWatch site ID.
        site_code: Site code from ``V_Site``.
        log_date: Business (paid) date.
        item_id: ``V_Item.ObjID``. None unless ``group_by="item"``.
        item_name: ``V_Item.Name``. None unless ``group_by="item"``. Treat as internal:
            "Wash Notes" items can carry a personal name.
        category: ``V_ItemRptCategory.Name``. None when ``group_by="base_type"``.
        category_branch: Report-category branch, e.g. ``"001001001"`` (Categories > Wash >
            Basic Washes). None when ``group_by="base_type"``.
        base_type: Item base type (120 Service, 140 Prepaid, 150 CouponDisc, 160 Paidout,
            170 Deposit, 180 Tender, ...).
        base_type_name: Base type name from ``BASE_TYPES``.
        lines: Number of sale lines.
        quantity: ``SUM(Qty)``.
        amount: ``SUM(Amt)`` signed as stored (discounts and tenders are negative).
    """

    site_id: int
    site_code: str
    log_date: date
    item_id: int | None = None
    item_name: str | None = None
    category: str | None = None
    category_branch: str | None = None
    base_type: int
    base_type_name: str
    lines: int
    quantity: float
    amount: float


class TenderTotal(SiteWatchModel):
    """Money received by tender category (and optionally tender item).

    Attributes:
        site_id: SiteWatch site ID.
        site_code: Site code from ``V_Site``.
        log_date: Business (paid) date.
        category: Tender report category ("Credit Card", "Cash", "House Accounts", ...).
        category_branch: Report-category branch.
        tender: Tender item name ("Visa / MC", "American Express"); None unless
            ``by_item=True``.
        lines: Number of tender lines (not customers or transactions).
        amount: Money received, positive (``-SUM(Amt)``).
    """

    site_id: int
    site_code: str
    log_date: date
    category: str
    category_branch: str
    tender: str | None = None
    lines: int
    amount: float


class SaleLine(SiteWatchModel):
    """One line of a sale (``V_SaleItems``). The free-text ``Note`` column is never read.

    Attributes:
        seq: Line sequence within the sale.
        leader_seq: ``Seq`` of the package leader line, for package member lines.
        item_id: ``V_Item.ObjID``.
        item_name: ``V_Item.Name``.
        category: ``V_ItemRptCategory.Name``.
        base_type: Item base type.
        is_tender: True for base type 180.
        quantity: ``V_SaleItems.Qty``.
        unit_value: ``V_SaleItems.Val``.
        amount: ``V_SaleItems.Amt``; None on note and header lines.
    """

    seq: int
    leader_seq: int | None = None
    item_id: int
    item_name: str | None = None
    category: str | None = None
    base_type: int
    is_tender: bool
    quantity: float | None = None
    unit_value: float | None = None
    amount: float | None = None


class Sale(SiteWatchModel):
    """A sale header (SWSLS05 style) with optional lines.

    Attributes:
        site_id: SiteWatch site ID.
        site_code: Site code from ``V_Site``.
        sale_id: ``V_Sale.ObjID`` (internal, not chronological).
        receipt_number: ``f_SaleScr(ObjID)``, the number printed on the receipt.
        code: ``V_Sale.Code``.
        status_code: ``f_StatusCode(Status)``: ``C:P``, ``C:PW``, ``I:P-W``, ``Notes``,
            ``PendAppr``. ``E:-P`` never appears (its LogDate is NULL).
        is_completed: ``f_IsCompleted(Status) = 1``.
        created_at: When the sale was started.
        log_date: Business (paid) date.
        paid_at: ``LogDate + LogTime / 14400.0`` (site-local wall-clock time).
        hour: Paid hour (``V_SaleStats.Hr``).
        shift: Shift number.
        terminal_id: ``V_Terminal.ObjID``.
        terminal_name: Terminal name, e.g. "Automatic Recharge", "XPT1".
        customer_code_id: ``V_CustomerCode.ObjID`` used on the sale (ID only, no plate).
        customer_id: ``V_CustomerCode.Customer`` (ID only).
        actual_sale_id: Raw ``V_Sale.ActualSaleID`` (set on ARM recharge chains; not an
            "original sale" filter).
        total: ``V_Sale.Total`` (sum of the non-tender lines).
        items: Sale lines, ordered by ``seq``. Empty when ``include_items=False``.
    """

    site_id: int
    site_code: str
    sale_id: int
    receipt_number: str
    code: str | None = None
    status_code: str
    is_completed: bool
    created_at: datetime | None = None
    log_date: date | None = None
    paid_at: datetime | None = None
    hour: int | None = None
    shift: int | None = None
    terminal_id: int | None = None
    terminal_name: str | None = None
    customer_code_id: int | None = None
    customer_id: int | None = None
    actual_sale_id: int | None = None
    total: float
    items: list[SaleLine] = Field(default_factory=list)


class StatusCount(SiteWatchModel):
    """Sale count and total for one status code on one business day.

    Attributes:
        site_id: SiteWatch site ID.
        site_code: Site code from ``V_Site``.
        log_date: Business (paid) date.
        status_code: ``f_StatusCode(Status)``.
        is_completed: ``f_IsCompleted(Status) = 1``.
        sales: Sale headers.
        total: ``SUM(V_Sale.Total)``.
    """

    site_id: int
    site_code: str
    log_date: date
    status_code: str
    is_completed: bool
    sales: int
    total: float
