"""Catalog resource: items, report categories and terminals."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from sitewatch_data_client._constants import BASE_TYPES
from sitewatch_data_client._exceptions import ValidationError
from sitewatch_data_client._filters import Sites, build_in_clause, normalize_sites
from sitewatch_data_client.types._catalog import Item, ReportCategory, Terminal

if TYPE_CHECKING:
    import pandas as pd

    from sitewatch_data_client._client import SiteWatchClient

# SQL constants. Only library-owned text and ``?`` placeholders are ever added to these.

ITEMS_SQL = """
SELECT i.ObjID, i.Name, i.Number, i.ItemType, t.Name AS ItemTypeName, t.BaseType,
       i.ReportCategory, rc.Name AS Category, rc.Branch, i.ProfitCenter, i.IsPackage,
       i.PlanType, i.ActiveAt, i.Enabled
FROM V_Item i
JOIN V_ItemType t ON t.ObjID = i.ItemType
JOIN V_ItemRptCategory rc ON rc.ObjID = i.ReportCategory
WHERE 1 = 1{filters}
ORDER BY rc.Branch, i.Name, i.ObjID
""".strip()

ITEMS_ENABLED_FILTER = " AND i.Enabled = 1"
ITEMS_BRANCH_FILTER = " AND rc.Branch STARTING WITH ?"

CATEGORY_BRANCH_LOOKUP_SQL = (
    "SELECT rc.Branch FROM V_ItemRptCategory rc WHERE UPPER(TRIM(rc.Name)) = UPPER(?)"
)

REPORT_CATEGORIES_SQL = (
    "SELECT rc.ObjID, rc.Name, rc.Branch FROM V_ItemRptCategory rc ORDER BY rc.Branch"
)

TERMINALS_SQL = """
SELECT t.ObjID, t.Site, st.Code AS SiteCode, t.Name, t.Number, t.Enabled
FROM V_Terminal t
LEFT JOIN V_Site st ON st.ID = t.Site
WHERE {site_clause}{filters}
ORDER BY t.Site, t.Number, t.ObjID
""".strip()

TERMINALS_ENABLED_FILTER = " AND t.Enabled = 1"

#: Every SQL constant in this module (scanned by the safety tests).
ALL_SQL: tuple[str, ...] = (
    ITEMS_SQL,
    ITEMS_ENABLED_FILTER,
    ITEMS_BRANCH_FILTER,
    CATEGORY_BRANCH_LOOKUP_SQL,
    REPORT_CATEGORIES_SQL,
    TERMINALS_SQL,
    TERMINALS_ENABLED_FILTER,
)

_BRANCH_SEGMENT = 3


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(isinstance(value, float) and math.isnan(value))
    except TypeError:
        return False


def _int(value: Any) -> int | None:
    return None if _is_null(value) else int(value)


def _text(value: Any) -> str | None:
    if _is_null(value):
        return None
    text = str(value).strip()
    return text or None


def _branch_depth(branch: str) -> int:
    return max(1, len(branch) // _BRANCH_SEGMENT)


def _parent_branch(branch: str) -> str | None:
    if len(branch) <= _BRANCH_SEGMENT:
        return None
    return branch[:-_BRANCH_SEGMENT]


class CatalogResource:
    """Item catalog, report category tree and terminal methods.

    Access via ``client.catalog``. These are configuration views: small, global (items and
    categories have no Site column) and cheap to read. No date range is needed.
    """

    def __init__(self, client: SiteWatchClient) -> None:
        self._client = client

    # -- helpers ----------------------------------------------------------------------------

    def _resolve_category_branch(self, name: str) -> str:
        """Resolve a report category name to its ``Branch`` (spec 7.3a).

        Raises:
            ValidationError: If ``name`` is blank, matches no category, or matches more
                than one.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("report_category must be a non-empty category name.")
        df = self._client._execute(CATEGORY_BRANCH_LOOKUP_SQL, [name.strip()])
        branches = [] if df.empty else [_text(b) for b in df["branch"]]
        branches = [b for b in branches if b]
        if not branches:
            raise ValidationError(
                f"Unknown report category: '{name.strip()}'. "
                "Use client.catalog.report_categories() to list the names."
            )
        if len(branches) > 1:
            raise ValidationError(
                f"Report category name '{name.strip()}' is ambiguous "
                f"({len(branches)} categories share it)."
            )
        return branches[0]

    @staticmethod
    def _row_to_item(r: pd.Series) -> Item:
        base_type = int(r["basetype"])
        return Item(
            item_id=int(r["objid"]),
            name=_text(r["name"]) or "",
            number=_int(r["number"]),
            item_type_id=int(r["itemtype"]),
            item_type=_text(r["itemtypename"]) or "",
            base_type=base_type,
            base_type_name=BASE_TYPES.get(base_type, f"BaseType {base_type}"),
            report_category_id=int(r["reportcategory"]),
            report_category=_text(r["category"]) or "",
            category_branch=_text(r["branch"]) or "",
            profit_center=_int(r["profitcenter"]),
            is_package=_int(r["ispackage"]) == 1,
            plan_type_id=_int(r["plantype"]),
            active_at=_int(r["activeat"]),
            enabled=_int(r["enabled"]) == 1,
        )

    @staticmethod
    def _row_to_category(r: pd.Series) -> ReportCategory:
        branch = _text(r["branch"]) or ""
        return ReportCategory(
            category_id=int(r["objid"]),
            name=_text(r["name"]) or "",
            branch=branch,
            depth=_branch_depth(branch),
            parent_branch=_parent_branch(branch),
        )

    @staticmethod
    def _row_to_terminal(r: pd.Series) -> Terminal:
        return Terminal(
            terminal_id=int(r["objid"]),
            site_id=int(r["site"]),
            site_code=_text(r["sitecode"]),
            name=_text(r["name"]),
            number=_int(r["number"]),
            enabled=_int(r["enabled"]) == 1,
        )

    # -- public methods -----------------------------------------------------------------------

    def items(
        self, *, include_disabled: bool = False, report_category: str | None = None
    ) -> list[Item]:
        """List the item catalog with item type, base type and report category.

        Items are global (``V_Item`` has no Site column); about 4,800 rows in total. Where an
        item is offered is in ``active_at`` (raw ``ActiveAt``: above 0 is a site, below 0 a
        negated site group, -2147483647 all sites, 0 unassigned). Prices are not included in
        v1 because their effective-date semantics are unverified.

        The joins ``V_Item -> V_ItemType`` and ``V_Item -> V_ItemRptCategory`` are verified
        0-orphan. One statement (two when ``report_category`` is given), well under a second.

        Args:
            include_disabled: Include items with ``Enabled = 2``. Disabled items still appear
                on historical sales, so include them when labelling old sale lines.
            report_category: Optional report category **name** (case-insensitive). Matches
                the category and all of its descendants, so ``"Wash"`` returns the whole Wash
                tree. The name is resolved to its branch first, then filtered with
                ``rc.Branch STARTING WITH ?``.

        Returns:
            Items ordered by category branch, then name, then item ID.

        Raises:
            ValidationError: If ``report_category`` is blank, unknown or ambiguous.
            ConnectionError: If the client is not connected.
            QueryError: If Firebird rejects the query.

        Examples:
            >>> washes = client.catalog.items(report_category="Wash")
            >>> services = [i for i in client.catalog.items() if i.base_type == 120]
        """
        if not isinstance(include_disabled, bool):
            raise ValidationError("include_disabled must be a bool.")
        filters = ""
        params: list[Any] = []
        if not include_disabled:
            filters += ITEMS_ENABLED_FILTER
        if report_category is not None:
            branch = self._resolve_category_branch(report_category)
            filters += ITEMS_BRANCH_FILTER
            params.append(branch)
        sql = ITEMS_SQL.format(filters=filters)
        df = self._client._execute(sql, params or None)
        return [self._row_to_item(r) for _, r in df.iterrows()]

    def report_categories(self) -> list[ReportCategory]:
        """List the item report category tree (``V_ItemRptCategory``, 52 nodes).

        ``Branch`` is a path of 3-digit levels: ``"001"`` is the root (``Categories``),
        ``"001001"`` is ``Wash`` and ``"001001001"`` is ``Basic Washes``. ``depth`` and
        ``parent_branch`` are derived from it in Python. Category names are unique
        case-insensitively (verified), which is what ``items(report_category=...)`` relies on.

        Returns:
            Categories ordered by branch, so parents come before their children.

        Raises:
            ConnectionError: If the client is not connected.
            QueryError: If Firebird rejects the query.

        Examples:
            >>> for c in client.catalog.report_categories():
            ...     print("  " * (c.depth - 1) + c.name)
        """
        df = self._client._execute(REPORT_CATEGORIES_SQL)
        return [self._row_to_category(r) for _, r in df.iterrows()]

    def terminals(self, sites: Sites = None, *, include_disabled: bool = False) -> list[Terminal]:
        """List lane and POS terminals per site (``V_Terminal``).

        ``V_Sale.Terminal`` points to ``terminal_id`` (``ObjID``). Join on that ID, never on
        ``number``: terminal numbers are not unique per site because disabled duplicates
        exist. Recharges post on the terminal named ``"Automatic Recharge"``. One small
        statement.

        Args:
            sites: Site ID or IDs. Defaults to the ICON car-wash sites
                (5, 13, 18, 20, 24, 25).
            include_disabled: Include terminals with ``Enabled = 2``. Historical sales can
                reference disabled terminals.

        Returns:
            Terminals ordered by site, number and terminal ID.

        Raises:
            ValidationError: If ``sites`` is invalid.
            ConnectionError: If the client is not connected.
            QueryError: If Firebird rejects the query.

        Examples:
            >>> lanes = client.catalog.terminals(24)
            >>> by_id = {t.terminal_id: t.name for t in client.catalog.terminals()}
        """
        if not isinstance(include_disabled, bool):
            raise ValidationError("include_disabled must be a bool.")
        site_ids = normalize_sites(sites)
        site_clause, params = build_in_clause("t.Site", site_ids)
        filters = "" if include_disabled else TERMINALS_ENABLED_FILTER
        sql = TERMINALS_SQL.format(site_clause=site_clause, filters=filters)
        df = self._client._execute(sql, params)
        return [self._row_to_terminal(r) for _, r in df.iterrows()]
