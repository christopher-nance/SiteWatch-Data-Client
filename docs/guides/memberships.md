# Memberships (ARM)

SiteWatch calls its recurring monthly memberships **ARM** (Automatic Recurring Membership).
All membership methods are accessed via `client.memberships`:

| Method | Returns | Range limit | Source |
|---|---|---|---|
| `plans(*, include_disabled=False)` | `list[MembershipPlan]` | none | `V_PlanType` |
| `activity(start, end, sites=None, *, by_plan=True)` | `list[MembershipActivity]` | 93 days | `V_ARM_Monthly_Activity` |
| `activity_totals(start, end, sites=None)` | `MembershipActivityTotals` | 93 days | via `activity` |
| `new_members(start, end, sites=None, *, include_contact=False, include_recharge_site=False, as_of=None, first_join_only=False)` | `list[NewMember]` | 31 days | `V_SalePasses`, `Arm_SalePass_Member` |
| `member_status(customer_id, as_of=None)` | `MemberStatus \| None` | n/a | `Arm_SalePass_Member` |
| `events(start, end, site, *, statuses=None)` | `list[MembershipEvent]` | 7 days | `V_Sale` + `V_SalePasses` |

`sites=None` means the ICON car-wash sites (5, 13, 18, 20, 24, 25). Dates are inclusive and
accept a `date`, a `datetime` or an ISO `YYYY-MM-DD` string.

!!! note "Examples use made-up values"
    Every number, name and ID on this page is invented for illustration.

## ARM status codes

Every membership event is a `V_SalePasses` row with a `Status` code. The codes are in
`sitewatch_data_client._constants.ARM_STATUS`. The names are the exact text the server function
`f_ArmStatusName` returns (and so what `status_name` / `pass_status_name` hold), checked live:

| Code | `f_ArmStatusName` | Sale status it appears on |
|---|---|---|
| 0 | Used | `C:PW`, `I:P-W` |
| 1 | Joined | `C:PW`, `I:P-W` |
| 2 | Transfer In | `C:PW`, `I:P-W` |
| 3 | Renewed (the monthly recharge) | `C:P` |
| 6 | Resumed | |
| 7 | Discontinuing | `C:P`, `C:PW`, `I:P-W` |
| 21 | Expired | `Notes` |
| 27 | Discontinued | `Notes` |
| 29 | Terminated | `C:P` |
| 30 | Transfer Out | `C:PW`, `I:P-W` |
| 40 | Suspended | |
| 110 | CC Expired (card expired) | `Notes` |
| 120 | CC Declined (card declined) | `Notes` |
| 130 | Recharge Problem | |
| 140 | CC Information Required (enter or swipe a new card) | `Notes` |
| 150 | CC Approved (new card approved) | |
| 200 | Credit Card Changed | `Notes` |
| 201 | Join Date Changed | |

!!! info "Paid means completed **or** `I:P-W`"
    A join or a member wash on a sale whose wash is still pending (`I:P-W`) is paid. SiteWatch's
    ARM roll-up counts it, and so does every method here, so `new_members` and
    `activity_totals().joins` count the same joins (see below for when the totals differ).

## Plans

ARM plans are `V_PlanType` rows with `Typ = 110`. Plan types are global (there is no site
column), so legacy plans appear too, and **the view has no price**: what a member pays comes from
the sale (`NewMember.billing_amount`, `MembershipEvent.amount`).

```python
for plan in client.memberships.plans():
    print(plan.plan_type_id, plan.name, plan.expiration_age_days)
# 901 Unlimited Example 0
# 902 Family Example 0
```

`include_disabled=True` adds plans with `Enabled = 2`.

## Daily activity

`activity()` reads `V_ARM_Monthly_Activity`, which is **daily** despite its name. It gives joins,
recharges, upsells, washes used, transfers and every churn counter per site, day and (by default)
plan. It is cheap: two weeks at all six sites aggregates in about a second.

```python
rows = client.memberships.activity("2026-10-05", "2026-10-11", sites=24)
for r in rows[:2]:
    print(r.log_date, r.plan_name, r.joins, r.recharges, r.washes_used, r.declined)
# 2026-10-05 Unlimited Example 1 12 40 30
# 2026-10-05 Family Example 0 3 9 4
```

Pass `by_plan=False` for one row per site and day.

!!! warning "Not an active-member count"
    The view has one row per leader *who had activity that day*. Its row count, or a sum of
    its rows, is not the number of active members.

### Period totals

```python
t = client.memberships.activity_totals("2026-10-01", "2026-10-14")
print(t.joins, t.recharges, t.recharge_amount, t.declined)
# 412 5120 151234.56 12001
for m in t.joins_by_site:
    print(m.site_code, int(m.value))
# ICONTN-005 70
# ICONTN-015 55
# ...
```

`joins_by_site` lists every requested site, with 0 for a site that had no joins.

### Use case: churn ratio for a month

```python
t = client.memberships.activity_totals("2026-09-01", "2026-09-30", sites=24)
lost = t.terminated + t.expired + t.discontinued
print(f"joins {t.joins}, lost {lost}, net {t.joins - lost:+}")
```

## New members

`new_members()` is the library version of the `export_members.ps1` export. It returns every
customer with a **paid join** (`Status = 1` on a completed or `I:P-W` sale) at the requested sites
in the window, **including rejoins**, with their **current** plan, status, billing amount, next
bill date, member-since date, family size, last wash, washes since joining and last charge.

```python
members = client.memberships.new_members("2026-10-05", "2026-10-11", sites=24, as_of="2026-10-11")
print(len(members))
# 24
m = members[0]
print(m.customer_id, m.join_date, m.plan_name, m.pass_status_name, m.billing_amount, m.next_bill_date)
# 100001 2026-10-05 Unlimited Example Joined 19.99 2026-11-05
```

### Rejoins and first-time members

Returning members are common: in one sample week about one join in five was a customer who had
been a member before (some first joined years earlier). `prior_joins` counts the customer's paid
ARM joins at any site before `join_date`, and `is_rejoin` is `prior_joins > 0`. Pass
`first_join_only=True` to keep first-time members only.

```python
members = client.memberships.new_members("2026-10-05", "2026-10-11", sites=24)
rejoins = [m for m in members if m.is_rejoin]
first_timers = client.memberships.new_members(
    "2026-10-05", "2026-10-11", sites=24, first_join_only=True
)
```

A customer appears **once**, even with several joins in the window (at one or several requested
sites). `join_date` and `join_site_id` describe the earliest of them, by date and time. So
`len(new_members(...))` equals `activity_totals(...).joins` only when nobody joined twice in the
window.

### `as_of` is end-of-day

The current state comes from `Arm_SalePass_Member(customer, as_of)`. A date is sent as
**23:59:59** of that day. Sending midnight would miss everyone who joined on the last day of the
window (they were not members yet at 00:00). `as_of=None` means "now" on the server; pass
`as_of=end` for a reproducible snapshot.

### Contact details are opt-in

Names, addresses, phones, emails and plates come back only with `include_contact=True`, in
`NewMember.contact`. Card numbers, card expiry and birthdate are never selected.

```python
members = client.memberships.new_members(
    "2026-10-05", "2026-10-11", sites=24, include_contact=True
)
c = members[0].contact
print(c.full_name, c.email, c.codes)
# Pat Example pat@example.com ['TEST123']
```

!!! danger "Personal data"
    Treat `contact` as confidential. Keep it out of logs, tickets, chat and shared drives.

### Recharge site

`include_recharge_site=True` adds `recharge_site_id`/`recharge_site_code`: the site of the
member's latest paid join or recharge (the export's "latest charge site"). It costs an extra
correlated subquery per member (about 3 to 4 seconds for a week at one site).

### Performance

About a second for one site over a week (two dozen members), about 3 s for two weeks. A 31-day,
six-site call (around 1,000 joins) takes roughly a minute. For interactive use, query one site, or
14 days or less.

### Export to CSV

```python
from pathlib import Path

import pandas as pd

members = client.memberships.new_members("2026-10-01", "2026-10-14", sites=24, as_of="2026-10-14")
df = pd.DataFrame([m.model_dump(exclude={"contact"}) for m in members])
Path("exports").mkdir(exist_ok=True)  # exports/ is gitignored
df.to_csv("exports/new_members.csv", index=False)
```

## One member's status

```python
status = client.memberships.member_status(100001, as_of="2026-10-14")
if status is None:
    print("not a member")
else:
    print(status.plan_name, status.pass_status_name, status.sale_status_code, status.expires)
# Unlimited Example Renewed C:P 2026-11-05
```

No personal data is returned. The procedure is fast for new members but grows with a member's
history, so do not loop `member_status` over hundreds of customers; use `new_members` instead.

## Row-level events

`events()` returns the `V_SalePasses` rows behind `activity()` for **one site**, up to 7 days, with
IDs only.

```python
events = client.memberships.events("2026-10-05", "2026-10-11", site=24, statuses=[120, 130])
print(len(events))
# 1040
e = events[0]
print(e.event_at, e.sale_status_code, e.status_name, e.customer_id, e.amount)
# 2026-10-05 01:12:00 Notes CC Declined 100001 19.99
```

`statuses` filters by ARM status code (an int or a list). `sale_status_code` lets you split by sale
status: joins and uses are on `C:PW`/`I:P-W`, recharges and terminations on `C:P`, declines,
expiries and discontinues on `Notes`.

### Duplicate rows

Duplicate rows per sale are **kept**: `(site, sale_id, seq)` is not unique, and `seq` can tie at 0.
SiteWatch's own reports use the highest `seq` per sale, with ties broken on status. To apply that
rule:

```python
latest = {}
for e in events:
    key = (e.site_id, e.sale_id)
    if key not in latest or (e.seq or 0, e.status) > (latest[key].seq or 0, latest[key].status):
        latest[key] = e
```

## What is not available, and why

| Want | Status | Why |
|---|---|---|
| Active member count (`Arm_Monthly_Current_Status`) | not a method | It computes every member at every site. A `FIRST 5` took 38 s and a grouped count did not finish in 200 s. Its output also includes members who dropped in the prior 2 months, and legacy sites. |
| Start/end member counts (`Arm_Summary_For_Date_Range`) | future work | One week timed out after 175 s. A possible opt-in, 1-day `summary()` may follow once it has been timed off-hours. |
| Club plan balances (`CustClubPlan_Balances`) | not a method | It timed out on one week, and club plans are not ARM. |
| Plan prices | not in `V_PlanType` | Use the sale amounts (`billing_amount`, `events().amount`). |

If you really need the current roster, ask DRB for a scheduled ARM Summary export, or run the
procedure yourself through the raw-query escape hatch **off-hours only**, one site at a time.

!!! danger "Measured: about 8 minutes for one site"
    A `FIRST 100` call for one site took **462 seconds** on the shared production server, and
    `query()` has no statement timeout or cancel. `query()` logs a warning whenever the SQL calls
    `Arm_Monthly_Current_Status`, `Arm_Summary_For_Date_Range` or `CustClubPlan_Balances`. The
    snippet below is deliberately guarded so that copying it does nothing until you opt in.

```python
from datetime import date

OFF_HOURS = False  # set to True only outside business hours

if OFF_HOURS:
    df = client.query(
        "SELECT FIRST 100 m.RechargeSite, m.Status, m.PlanName "
        "FROM Arm_Monthly_Current_Status(?) m WHERE m.RechargeSite = ?",
        [date(2026, 10, 14), 24],
    )
```
