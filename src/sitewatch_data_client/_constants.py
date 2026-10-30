"""Library constants: ICON site IDs, SiteWatch codes and per-method date-range limits."""

from __future__ import annotations

# --- Sites ---------------------------------------------------------------------------------

#: ICON car-wash sites (the default for every ``sites=None`` argument).
ICON_SITE_IDS: tuple[int, ...] = (5, 13, 18, 20, 24, 25)
#: ICON office site (no sales or punches).
ICON_OFFICE_SITE_ID = 99
#: Home sites of office accounts. Their employees clock in at car-wash sites but rarely
#: clock out, so they are left out of labor-productivity hours by default.
OFFICE_HOME_SITE_IDS: tuple[int, ...] = (ICON_OFFICE_SITE_ID,)
#: Time zone of the ICON business dates (``LogDate`` / ``PunchDate``).
BUSINESS_TIME_ZONE = "America/Chicago"
#: ICON query server site (no sales or punches).
ICON_QUERY_SERVER_SITE_ID = 100
#: Every ICON site including the office and the query server.
ICON_ALL_SITE_IDS: tuple[int, ...] = (
    *ICON_SITE_IDS,
    ICON_OFFICE_SITE_ID,
    ICON_QUERY_SERVER_SITE_ID,
)

# --- Items -----------------------------------------------------------------------------------

#: ``V_ItemBaseType`` IDs and names.
BASE_TYPES: dict[int, str] = {
    100: "Note",
    105: "Hidden Note",
    110: "Merchandise",
    120: "Service",
    130: "Fuel",
    140: "Prepaid",
    150: "CouponDisc",
    160: "Paidout",
    170: "Deposit",
    180: "Tender",
    190: "Tax",
    200: "Flag",
}
BASE_TYPE_DISCOUNT = 150
BASE_TYPE_PAIDOUT = 160
BASE_TYPE_DEPOSIT = 170
BASE_TYPE_TENDER = 180

# --- ARM memberships -------------------------------------------------------------------------

#: ``V_PlanType.Typ`` for ARM Monthly Pass plans.
ARM_PLAN_TYP = 110

#: ``V_SalePasses.Status`` codes with the exact text ``f_ArmStatusName`` returns (checked live).
ARM_STATUS: dict[int, str] = {
    0: "Used",
    1: "Joined",
    2: "Transfer In",
    3: "Renewed",
    6: "Resumed",
    7: "Discontinuing",
    21: "Expired",
    27: "Discontinued",
    29: "Terminated",
    30: "Transfer Out",
    40: "Suspended",
    110: "CC Expired",
    120: "CC Declined",
    130: "Recharge Problem",
    140: "CC Information Required",
    150: "CC Approved",
    200: "Credit Card Changed",
    201: "Join Date Changed",
}

# --- Punches ---------------------------------------------------------------------------------

#: ``ActualIn`` / ``ActualOut`` codes meaning the punch has no out time (OPEN).
PUNCH_OPEN_CODES: frozenset[int] = frozenset({30000, 30003, 30004})
#: Codes meaning the punch was edited in the Timecard Editor.
PUNCH_EDITED_CODES: frozenset[int] = frozenset({30001, 30002})
#: Day-change continuation.
PUNCH_DAY_CHANGE_CODE = -30000
#: Overtime split codes.
PUNCH_OT_SPLIT_CODES: frozenset[int] = frozenset({-30001, -30002})
#: Group punches are -1 .. -28800; real recorded times are 0 .. 28800 (tenths of a minute).
PUNCH_TENTHS_MAX = 28800
#: Pay weeks run Saturday to Friday (``WorkWeekStartDay = 7``). Python weekday() of Saturday.
PAY_WEEK_START_WEEKDAY = 5

# --- Date-range limits (inclusive days) --------------------------------------------------------

RANGE_LIMITS: dict[str, int] = {
    "sales.daily_summary": 31,
    "sales.totals": 31,
    "sales.hourly_summary": 14,
    "sales.item_sales": 31,
    "sales.tenders": 31,
    "sales.list": 3,
    "sales.status_mix": 14,
    "memberships.activity": 93,
    "memberships.new_members": 31,
    "memberships.events": 7,
    "labor.timecards": 31,
    "labor.timecard_totals": 31,
    "labor.labor_statistics": 31,
    "labor.worked_employees": 93,
    "labor.daily_hours": 93,
    "labor.productivity": 31,
}
