"""Catalog result models: items, report categories and terminals."""

from __future__ import annotations

from sitewatch_data_client.types._base import SiteWatchModel


class Item(SiteWatchModel):
    """One SiteWatch item (``V_Item``) with its type and report category.

    Items are global: there is no Site column. Where an item is offered is encoded in
    ``active_at``.

    Attributes:
        item_id: ``V_Item.ObjID``. This is the ID that ``V_SaleItems.Item`` points to.
        name: Item name, trimmed.
        number: Item number (``V_Item.Number``). Not unique; 0 is common.
        item_type_id: ``V_ItemType.ObjID``.
        item_type: ``V_ItemType.Name`` (for example ``"Lube Service"``).
        base_type: ``V_ItemType.BaseType`` (``V_ItemBaseType`` ID: 110 Merchandise,
            120 Service, 140 Prepaid, 150 CouponDisc, 160 Paidout, 170 Deposit, 180 Tender ...).
        base_type_name: Name of ``base_type`` (for example ``"Service"``).
        report_category_id: ``V_ItemRptCategory.ObjID``.
        report_category: Report category name (for example ``"Basic Washes"``).
        category_branch: Report category branch path, 3 digits per level
            (for example ``"001001001"``).
        profit_center: ``V_Item.ProfitCenter`` ID.
        is_package: True when the item is a package (a leader line with child lines).
            A NULL ``IsPackage`` maps to False.
        plan_type_id: ``V_PlanType`` ID for membership and prepaid items, otherwise None.
        active_at: Raw ``ActiveAt``: above 0 is a site ID, below 0 is a negated site group
            ID, -2147483647 means all sites, and 0 means unassigned.
        enabled: True when ``V_Item.Enabled = 1`` (1 enabled, 2 disabled).
    """

    item_id: int
    name: str
    number: int | None = None
    item_type_id: int
    item_type: str
    base_type: int
    base_type_name: str
    report_category_id: int
    report_category: str
    category_branch: str
    profit_center: int | None = None
    is_package: bool
    plan_type_id: int | None = None
    active_at: int | None = None
    enabled: bool


class ReportCategory(SiteWatchModel):
    """One node of the item report category tree (``V_ItemRptCategory``).

    Attributes:
        category_id: ``V_ItemRptCategory.ObjID`` (``V_Item.ReportCategory`` points here).
        name: Category name, trimmed (for example ``"Wash"``).
        branch: Branch path, 3 digits per level (``"001"`` is the root,
            ``"001001"`` is ``Wash``).
        depth: Number of 3-digit levels in ``branch`` (the root ``"001"`` has depth 1).
        parent_branch: Branch of the parent node, or None for the root.
    """

    category_id: int
    name: str
    branch: str
    depth: int
    parent_branch: str | None = None


class Terminal(SiteWatchModel):
    """One lane or POS terminal (``V_Terminal``).

    Attributes:
        terminal_id: ``V_Terminal.ObjID``. ``V_Sale.Terminal`` points here. Join on this,
            never on ``number``.
        site_id: ``V_Terminal.Site``.
        site_code: ``V_Site.Code`` (for example ``"ICONTN-030"``), or None if the site row
            is missing.
        name: Terminal name, trimmed (for example ``"Automatic Recharge"``).
        number: Terminal number. Not unique per site (disabled duplicates exist).
        enabled: True when ``V_Terminal.Enabled = 1`` (1 enabled, 2 disabled).
    """

    terminal_id: int
    site_id: int
    site_code: str | None = None
    name: str | None = None
    number: int | None = None
    enabled: bool
