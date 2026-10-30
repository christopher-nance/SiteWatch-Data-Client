# Catalog

Look up items, the report category tree and terminals with `client.catalog`. These are small configuration views, so none of the methods need a date range. Use them to label sale lines, to pick a `report_category` for `sales.item_sales()`, or to turn a terminal ID into a lane name.

| Method | Returns | Source views |
|--------|---------|--------------|
| `items(*, include_disabled=False, report_category=None)` | `list[Item]` | `V_Item`, `V_ItemType`, `V_ItemRptCategory` |
| `report_categories()` | `list[ReportCategory]` | `V_ItemRptCategory` |
| `terminals(sites=None, *, include_disabled=False)` | `list[Terminal]` | `V_Terminal`, `V_Site` |

Membership plans are covered by `client.memberships.plans()`, so they aren't repeated here.

## Basic Usage

```python
from dotenv import load_dotenv
from sitewatch_data_client import SiteWatchClient

load_dotenv()

with SiteWatchClient() as client:
    items = client.catalog.items()
    print(f"{len(items)} enabled items")

    for cat in client.catalog.report_categories():
        print("  " * (cat.depth - 1) + cat.name)

    for t in client.catalog.terminals(24):
        print(t.terminal_id, t.number, t.name)
```

## Items

`items()` returns the whole item catalog, about 4,800 rows. Items are **global**: `V_Item` has no Site column, and the same item ID is used at every site. Where an item is offered is stored in `active_at`.

- **`include_disabled`:** this is False by default, which keeps only `Enabled = 1` items. Pass True when you are labelling historical sale lines, because disabled items still appear on old sales.
- **`report_category`:** filter by a report category **name**, case-insensitively. It matches the category and all of its descendants, so `"Wash"` returns the whole Wash tree. The library first looks the name up to get its branch, then filters on `Branch STARTING WITH ?`. A name that matches no category raises `ValidationError`.

```python
with SiteWatchClient() as client:
    wash_items = client.catalog.items(report_category="Wash")
    basic = client.catalog.items(report_category="basic washes", include_disabled=True)
```

### Item Object Fields

| Field | Type | Description |
|-------|------|-------------|
| `item_id` | `int` | `V_Item.ObjID`, the ID that sale lines point to |
| `name` | `str` | Item name, trimmed |
| `number` | `int \| None` | Item number. It isn't unique, and 0 is common |
| `item_type_id` | `int` | `V_ItemType.ObjID` |
| `item_type` | `str` | Item type name |
| `base_type` | `int` | Base type ID (see below) |
| `base_type_name` | `str` | Base type name, e.g. `Service` |
| `report_category_id` | `int` | `V_ItemRptCategory.ObjID` |
| `report_category` | `str` | Category name, e.g. `Basic Washes` |
| `category_branch` | `str` | Category branch path, e.g. `001001001` |
| `profit_center` | `int \| None` | Profit center ID |
| `is_package` | `bool` | True for a package (a leader line with child lines). A NULL value is False |
| `plan_type_id` | `int \| None` | `V_PlanType` ID for membership and prepaid items |
| `active_at` | `int \| None` | Raw `ActiveAt` value (see below) |
| `enabled` | `bool` | `Enabled = 1`. SiteWatch uses 1 for enabled and 2 for disabled |

### Base Types

| ID | Name | Typical items |
|----|------|---------------|
| 100 / 105 | Note / Hidden Note | Comment lines |
| 110 | Merchandise | Retail goods |
| 120 | Service | Washes and extras |
| 140 | Prepaid | ARM plans, gift cards, wash books |
| 150 | CouponDisc | Coupons and discounts (negative amounts) |
| 160 | Paidout | ARM refunds and other paid-outs (negative) |
| 170 | Deposit | XPT acceptor deposits |
| 180 | Tender | Cash, card and other tenders |
| 190 | Tax | Tax lines |
| 200 | Flag | Marker lines |

### ActiveAt

`active_at` is returned raw rather than decoded:

| Value | Meaning |
|-------|---------|
| greater than 0 | A single site ID |
| less than 0 (other than -2147483647) | A site group: the group ID negated |
| -2147483647 | All sites |
| 0 | Not assigned (this doesn't mean inactive) |

```python
ALL_SITES = -2147483647

with SiteWatchClient() as client:
    items = client.catalog.items()
    site_24_only = [i for i in items if i.active_at == 24]
    everywhere = [i for i in items if i.active_at == ALL_SITES]
```

!!! note
    Prices aren't included in v1, because the effective-date rules for `STDPRICE` and `V_ItemValue1` haven't been verified yet.

## Report Categories

`report_categories()` returns the 52-node report category tree, ordered by branch so a parent always comes before its children. A `Branch` is a path made of 3-digit levels:

| Branch | Name | `depth` | `parent_branch` |
|--------|------|---------|-----------------|
| `001` | Categories | 1 | `None` |
| `001001` | Wash | 2 | `001` |
| `001001001` | Basic Washes | 3 | `001001` |

`depth` and `parent_branch` are worked out in Python from the branch. Category names are unique without regard to case, which is what lets `items(report_category=...)` and `sales.item_sales(report_category=...)` match on a name.

```python
with SiteWatchClient() as client:
    cats = client.catalog.report_categories()
    by_branch = {c.branch: c for c in cats}

    # Children of "Wash"
    wash = next(c for c in cats if c.name == "Wash")
    children = [c.name for c in cats if c.parent_branch == wash.branch]
```

### ReportCategory Object Fields

| Field | Type | Description |
|-------|------|-------------|
| `category_id` | `int` | `V_ItemRptCategory.ObjID` |
| `name` | `str` | Category name |
| `branch` | `str` | Branch path |
| `depth` | `int` | Number of 3-digit levels, where the root is 1 |
| `parent_branch` | `str \| None` | Parent's branch, or None for the root |

## Terminals

`terminals()` returns lane and POS terminals for the requested sites. By default that's the ICON car-wash sites (5, 13, 18, 20, 24 and 25).

- **Join on `terminal_id`**, which is `ObjID`. `V_Sale.Terminal` points to it. **Don't** join on `number`: terminal numbers aren't unique within a site, because disabled duplicates exist.
- **`include_disabled`:** this is False by default. Pass True when you are labelling historical sales, which can reference terminals that have since been disabled.
- Automatic ARM recharges post on the terminal named `Automatic Recharge`.

```python
from datetime import date

with SiteWatchClient() as client:
    names = {t.terminal_id: t.name for t in client.catalog.terminals(24, include_disabled=True)}

    df = client.query(
        "SELECT s.Terminal, COUNT(*) AS n FROM V_Sale s "
        "WHERE s.Site = ? AND s.LogDate >= ? AND s.LogDate < ? GROUP BY s.Terminal",
        [24, date(2026, 10, 5), date(2026, 10, 12)],
    )
    df["terminal_name"] = df["terminal"].map(names)
```

### Terminal Object Fields

| Field | Type | Description |
|-------|------|-------------|
| `terminal_id` | `int` | `V_Terminal.ObjID` |
| `site_id` | `int` | Site ID |
| `site_code` | `str \| None` | Site code from `V_Site`, e.g. `ICONTN-030` |
| `name` | `str \| None` | Terminal name |
| `number` | `int \| None` | Terminal number, which isn't unique |
| `enabled` | `bool` | `Enabled = 1` |

## Errors

| Situation | Exception |
|-----------|-----------|
| `report_category` is blank, unknown or ambiguous | `ValidationError` |
| `sites` is empty, not an int, or 0 or less | `ValidationError` |
| `include_disabled` isn't a bool | `ValidationError` |
| The client isn't connected | `ConnectionError` |
| Firebird rejects the query | `QueryError` |

## Performance

Each method runs one small statement. `items(report_category=...)` runs two: the name lookup and then the item query. Measured on 2026-10-30: `items()` took about 1 to 2 s, while `report_categories()` and `terminals()` each took about 0.2 s.
