"""Result models for ``client.memberships`` (ARM monthly-pass memberships)."""

from __future__ import annotations

from datetime import date, datetime

from sitewatch_data_client.types._base import SiteWatchModel
from sitewatch_data_client.types._common import SiteMetric

__all__ = [
    "MemberContact",
    "MemberStatus",
    "MembershipActivity",
    "MembershipActivityTotals",
    "MembershipEvent",
    "MembershipPlan",
    "NewMember",
]


class MembershipPlan(SiteWatchModel):
    """An ARM plan type (``V_PlanType`` with ``Typ = 110``).

    Plan types are global (no site column), so legacy plans appear too. ``V_PlanType`` has
    no price; a join's amount comes from the sale.

    Attributes:
        plan_type_id: ``V_PlanType.ObjID``.
        name: Plan name, e.g. ``"Unlimited Bronze"``.
        enabled: True when ``Enabled = 1`` (2 means disabled).
        expiration_age_days: ``ExpirationAge``. 0 means none, 9999 effectively never.
        recharge_plan_id: Forced migration target plan (rarely set).
        modified_at: Last modification timestamp (site-local wall-clock time).
    """

    plan_type_id: int
    name: str
    enabled: bool
    expiration_age_days: int | None = None
    recharge_plan_id: int | None = None
    modified_at: datetime | None = None


class MembershipActivity(SiteWatchModel):
    """Daily ARM roll-up for one site (and one plan when ``by_plan=True``).

    Sourced from ``V_ARM_Monthly_Activity``, which is **daily** despite its name. Counts
    are of paid sales (completed or ``I:P-W``), matching ``V_SalePasses`` by status.

    Attributes:
        site_id: SiteWatch site ID.
        site_code: ``V_Site.Code``.
        log_date: Business date.
        plan_type_id: ARM plan type, or None when ``by_plan=False``.
        plan_name: Plan name, or None when ``by_plan=False``.
        joins: New memberships sold (``SaleCount``).
        join_amount: Amount of new memberships sold.
        recharges: Monthly recharges (renewals).
        recharge_amount: Amount recharged.
        upsells: Plan upgrades.
        upsell_amount: Upgrade amount.
        washes_used: Member washes redeemed.
        transfers_in: Transfers in.
        transfers_out: Transfers out.
        suspended: Suspensions.
        resumed: Resumptions.
        terminated: Terminations.
        terminated_amount: Amount refunded on termination.
        expired: Expirations.
        discontinuing: Members flagged as discontinuing.
        discontinued: Discontinued members.
        declined: Declined recharge attempts.
        card_expiring: Card-expiring notices.
        recharge_problems: Recharge problems.
    """

    site_id: int
    site_code: str
    log_date: date
    plan_type_id: int | None = None
    plan_name: str | None = None
    joins: int = 0
    join_amount: float = 0.0
    recharges: int = 0
    recharge_amount: float = 0.0
    upsells: int = 0
    upsell_amount: float = 0.0
    washes_used: int = 0
    transfers_in: int = 0
    transfers_out: int = 0
    suspended: int = 0
    resumed: int = 0
    terminated: int = 0
    terminated_amount: float = 0.0
    expired: int = 0
    discontinuing: int = 0
    discontinued: int = 0
    declined: int = 0
    card_expiring: int = 0
    recharge_problems: int = 0


class MembershipActivityTotals(SiteWatchModel):
    """ARM activity summed over a period.

    Attributes:
        period_start: First business date (inclusive).
        period_end: Last business date (inclusive).
        joins: New memberships sold.
        recharges: Monthly recharges.
        recharge_amount: Amount recharged.
        terminated: Terminations.
        expired: Expirations.
        discontinued: Discontinued members.
        declined: Declined recharge attempts.
        joins_by_site: Joins per requested site (0 for a site with no activity).
    """

    period_start: date
    period_end: date
    joins: int = 0
    recharges: int = 0
    recharge_amount: float = 0.0
    terminated: int = 0
    expired: int = 0
    discontinued: int = 0
    declined: int = 0
    joins_by_site: list[SiteMetric] = []


class MemberContact(SiteWatchModel):
    """Customer contact details. Returned only with ``include_contact=True``.

    This is personal data: keep it out of logs, docs, chat and shared files.

    Attributes:
        first_name: First name.
        last_name: Last name.
        full_name: ``V_Customer.Name``.
        company: Company.
        address1: Address line 1.
        address2: Address line 2.
        city: City.
        state: State.
        zip: ZIP code.
        main_phone: Main phone.
        alt_phone: Alternate phone.
        email: Email address.
        last_code: Last plate or tag used (``LastCustCode``).
        codes: Every plate or tag on file (``V_CustomerCode``).
        suppress_mail: True when the customer opted out of mail.
        customer_created_at: When the customer record was created.
    """

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    company: str | None = None
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    main_phone: str | None = None
    alt_phone: str | None = None
    email: str | None = None
    last_code: str | None = None
    codes: list[str] = []
    suppress_mail: bool | None = None
    customer_created_at: datetime | None = None


class NewMember(SiteWatchModel):
    """A customer with a paid ARM join (first join or rejoin) at the requested sites.

    Current-state fields come from ``Arm_SalePass_Member(customer, as_of)``.

    Attributes:
        customer_id: ``V_Customer.ObjID``.
        join_date: Date of the customer's earliest paid join in the window.
        join_site_id: Site of that earliest join (by date and time).
        join_site_code: ``V_Site.Code`` of the join site.
        prior_joins: Paid ARM joins by this customer at any site before ``join_date``.
        is_rejoin: True when ``prior_joins > 0``: a returning member, not a first-time one.
        first_sale_site_id: ``FirstSaleSite`` of the current membership.
        plan_type_id: Current plan as of ``as_of``.
        plan_name: Current plan name.
        pass_status: Current ARM status code (see ``ARM_STATUS``).
        pass_status_name: Decoded status, e.g. ``"Joined"`` or ``"Terminated"``.
        billing_amount: Current billing amount (``Arm_SalePass_Member.Amount``).
        next_bill_date: ``Expires`` of the current pass, i.e. the next bill date.
        member_since: Member-since date.
        family_members: Number of members on the plan.
        last_used_date: Last member wash (any date, any site).
        uses_since_join: Member washes on or after the join date.
        last_charge_date: Last paid join or recharge.
        recharge_site_id: Site of the latest paid join or recharge. Only populated with
            ``include_recharge_site=True``.
        recharge_site_code: ``V_Site.Code`` of ``recharge_site_id``.
        contact: Contact details, only with ``include_contact=True``.
    """

    customer_id: int
    join_date: date
    join_site_id: int
    join_site_code: str
    prior_joins: int = 0
    is_rejoin: bool = False
    first_sale_site_id: int | None = None
    plan_type_id: int | None = None
    plan_name: str | None = None
    pass_status: int | None = None
    pass_status_name: str | None = None
    billing_amount: float | None = None
    next_bill_date: date | None = None
    member_since: date | None = None
    family_members: int | None = None
    last_used_date: date | None = None
    uses_since_join: int = 0
    last_charge_date: date | None = None
    recharge_site_id: int | None = None
    recharge_site_code: str | None = None
    contact: MemberContact | None = None


class MemberStatus(SiteWatchModel):
    """One member's ARM state from ``Arm_SalePass_Member``. Contains no personal data.

    Attributes:
        customer_id: The member (``Member``).
        leader_id: Plan leader (family plans).
        site_id: Site of the governing sale.
        sale_id: Governing sale ``ObjID``.
        plan_type_id: Plan type.
        plan_name: Plan name.
        pass_status: ARM status code.
        pass_status_name: Decoded ARM status.
        sale_status_code: Sale status code, e.g. ``C:P``.
        sale_paid_at: When the governing sale was paid.
        washes_sold: Washes sold on the pass.
        washes_used: Washes used.
        washes_left: Washes left.
        expires: Pass expiry (next bill date).
        first_sale_site_id: Site of the first sale.
        member_since: Member-since date.
        family_members: Number of members on the plan.
        amount: Billing amount.
        upsell: Upsell amount.
    """

    customer_id: int
    leader_id: int | None = None
    site_id: int | None = None
    sale_id: int | None = None
    plan_type_id: int | None = None
    plan_name: str | None = None
    pass_status: int | None = None
    pass_status_name: str | None = None
    sale_status_code: str | None = None
    sale_paid_at: datetime | None = None
    washes_sold: int | None = None
    washes_used: int | None = None
    washes_left: int | None = None
    expires: date | None = None
    first_sale_site_id: int | None = None
    member_since: date | None = None
    family_members: int | None = None
    amount: float | None = None
    upsell: float | None = None


class MembershipEvent(SiteWatchModel):
    """One ``V_SalePasses`` row (an ARM event) with its sale header. IDs only, no PII.

    Duplicate rows per sale are kept. SiteWatch reports use the highest ``seq`` per
    ``(site_id, sale_id)``, with ties broken on ``status``.

    Attributes:
        site_id: SiteWatch site ID.
        site_code: ``V_Site.Code``.
        sale_id: ``V_Sale.ObjID``.
        seq: ``V_SalePasses.Seq`` (can tie at 0).
        log_date: Business date.
        event_at: Paid timestamp (``LogDate + LogTime / 14400``), site-local time.
        sale_status_code: ``C:PW``, ``I:P-W``, ``C:P``, ``Notes`` ...
        customer_id: Customer ``ObjID``.
        leader_id: Plan leader ``ObjID``.
        plan_type_id: Plan type.
        plan_name: Plan name.
        status: ARM status code (see ``ARM_STATUS``).
        status_name: Decoded ARM status.
        washes: Washes on the pass row.
        expires: Pass expiry.
        family_members: Members on the plan.
        amount: Amount.
        upsell: Upsell amount.
        first_sale_site_id: Site of the first sale.
    """

    site_id: int
    site_code: str
    sale_id: int
    seq: int | None = None
    log_date: date
    event_at: datetime | None = None
    sale_status_code: str | None = None
    customer_id: int | None = None
    leader_id: int | None = None
    plan_type_id: int | None = None
    plan_name: str | None = None
    status: int
    status_name: str | None = None
    washes: int | None = None
    expires: date | None = None
    family_members: int | None = None
    amount: float | None = None
    upsell: float | None = None
    first_sale_site_id: int | None = None
