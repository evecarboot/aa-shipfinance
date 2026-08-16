"""Helper logic: interest calculation, insurance, invoice creation, notifications."""
import json
import logging
import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.utils import timezone

from . import app_settings
from .models import (
    AuditLog,
    BillingPeriod,
    DeliveryMode,
    FinanceAgreement,
    FinanceInstallment,
    FinanceOffer,
    FinanceStatus,
    InsuranceCoverage,
    InterestType,
    RentalAgreement,
    RentalStatus,
    ShipStock,
    ShipStockState,
)

logger = logging.getLogger(__name__)


def _round_isk(value):
    """Round an ISK amount to 2 decimal places (banks round, EVE uses .00)."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate_invoice_ref():
    """Generate an opaque, unique-ish invoice ref.

    Format: SF-XXXXXX where X is alphanumeric. Opaque by design for op-sec —
    the ref appears in corp wallet journal entries and should not reveal what
    it's for. Admins can change the prefix via SHIPFINANCE_INVOICE_REF_PREFIX.
    """
    prefix = app_settings.SHIPFINANCE_INVOICE_REF_PREFIX
    token = secrets.token_hex(4).upper()  # 8 chars
    ref = f"{prefix}-{token}"
    # Keep within invoices' 72-char limit (always will be, but be safe)
    return ref[:72]


def create_invoice(character, amount, due_date, note=""):
    """Create an Invoice via the invoices plugin.

    Returns the Invoice instance, or None if invoices is not installed.
    """
    if not app_settings.invoices_installed():
        logger.error("invoices plugin not installed; cannot create Invoice")
        return None
    from invoices.models import Invoice
    ref = generate_invoice_ref()
    inv = Invoice.objects.create(
        character=character,
        amount=_round_isk(amount),
        invoice_ref=ref,
        due_date=due_date,
        note=note,
    )
    logger.info(f"Created invoice {ref} for {character} amount {amount}")
    return inv


# ---------------------------------------------------------------------------
# Interest / installment schedule
# ---------------------------------------------------------------------------

def compute_finance_schedule(principal, term_months, interest_type, interest_rate):
    """Compute (total_amount, monthly_payment) for a finance offer.

    - FLAT: total = principal * (1 + rate/100), monthly = total / term
    - APR:  amortizing monthly payment via standard formula, total = monthly * term
            monthly = P * (r * (1+r)^n) / ((1+r)^n - 1)
            where r = (rate/100)/12, n = term_months
    """
    principal = Decimal(principal)
    term = int(term_months)
    rate = Decimal(interest_rate)

    if interest_type == InterestType.FLAT:
        total = principal * (Decimal("1") + rate / Decimal("100"))
        monthly = total / term if term else total
    elif interest_type == InterestType.APR:
        monthly_rate = (rate / Decimal("100")) / Decimal("12")
        if monthly_rate == 0:
            monthly = principal / term if term else principal
        else:
            factor = (Decimal("1") + monthly_rate) ** term
            monthly = principal * (monthly_rate * factor) / (factor - Decimal("1"))
        total = monthly * term
    else:
        raise ValueError(f"Unknown interest_type: {interest_type}")

    return _round_isk(total), _round_isk(monthly)


def build_installment_schedule(finance_agreement):
    """Create FinanceInstallment rows + Invoices for a FinanceAgreement.

    Called after the agreement is created and the ship is contracted out.
    Returns the list of FinanceInstallment objects.
    """
    installments = []
    now = timezone.now()
    for i in range(1, finance_agreement.term_months + 1):
        due = now + timedelta(days=30 * i)
        inv = create_invoice(
            character=finance_agreement.member_character,
            amount=finance_agreement.monthly_payment,
            due_date=due,
            note=f"Ship Finance installment {i}/{finance_agreement.term_months} - "
                 f"{finance_agreement.ship_stock.doctrine_fit.name}",
        )
        if inv is None:
            logger.error(f"Failed to create invoice for installment {i} of "
                         f"finance {finance_agreement.id}")
            break
        inst = FinanceInstallment.objects.create(
            finance_agreement=finance_agreement,
            invoice=inv,
            installment_number=i,
            amount=finance_agreement.monthly_payment,
            due_date=due,
            is_final=(i == finance_agreement.term_months),
        )
        installments.append(inst)
    return installments


# ---------------------------------------------------------------------------
# Insurance
# ---------------------------------------------------------------------------

def compute_insurance_premium(principal, premium_rate):
    """Insurance premium = principal * (premium_rate / 100)."""
    return _round_isk(Decimal(principal) * (Decimal(premium_rate) / Decimal("100")))


def create_insurance_invoice(finance_agreement):
    """Create the one-time insurance premium invoice at finance signing."""
    if not finance_agreement.insurance_purchased:
        return None
    inv = create_invoice(
        character=finance_agreement.member_character,
        amount=finance_agreement.insurance_premium,
        due_date=timezone.now() + timedelta(days=7),
        note=f"Ship Finance insurance premium - {finance_agreement.ship_stock.doctrine_fit.name}",
    )
    finance_agreement.insurance_invoice = inv
    finance_agreement.save()
    return inv


# ---------------------------------------------------------------------------
# Rental fee calculation
# ---------------------------------------------------------------------------

def compute_rental_fee(rate, billing_period, duration_timedelta):
    """Compute rental fee based on rate, period, and actual duration.

    For contract/hangar_request: fee is rate * number of periods in the
    agreed rental window (start to due_date).
    For free-use: fee is rate * number of periods in detected duration
    (take to return).
    """
    period_td = BillingPeriod.to_timedelta(billing_period)
    if period_td.total_seconds() <= 0:
        return Decimal("0")
    periods = Decimal(duration_timedelta.total_seconds()) / Decimal(
        period_td.total_seconds())
    # Round up to whole periods (you rent for 1.2 days, you pay for 2)
    import math
    periods = Decimal(math.ceil(float(periods)))
    return _round_isk(Decimal(rate) * periods)


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------

def log_action(action, performed_by=None, rental_agreement=None,
               finance_agreement=None, ship_stock=None, detail=""):
    AuditLog.objects.create(
        action=action,
        performed_by=performed_by,
        rental_agreement=rental_agreement,
        finance_agreement=finance_agreement,
        ship_stock=ship_stock,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify_member(member, title, message, eve_character=None):
    """Send an Auth notification to a member (and optionally Discord).

    Falls back gracefully if aadiscordbot is not installed.
    """
    if app_settings.SHIPFINANCE_SEND_AUTH_NOTIFICATIONS:
        try:
            from allianceauth.notifications import notify as auth_notify
            auth_notify(member, title, message, "info")
        except Exception as e:
            logger.error(f"Auth notify failed: {e}")

    if (app_settings.SHIPFINANCE_SEND_DISCORD_NOTIFICATIONS
            and app_settings.discord_bot_active() and eve_character):
        try:
            from aadiscordbot.tasks import send_message
            from discord import Embed, Color
            url = f"{app_settings.get_site_url()}"
            e = Embed(title=title, description=message, url=url, color=Color.blue())
            send_message(user=member, embed=e)
        except Exception as e:
            logger.error(f"Discord notify failed: {e}")


# Admin webhook colors by event type
_WEBHOOK_COLORS = {
    "rental_created": 0x3498db,      # blue
    "rental_returned": 0x2ecc71,     # green
    "rental_overdue": 0xe74c3c,      # red
    "rental_destroyed": 0xe74c3c,    # red
    "rental_lost": 0x95a5a6,         # gray
    "finance_created": 0x9b59b6,     # purple
    "finance_paid_off": 0x2ecc71,    # green
    "finance_defaulted": 0xe74c3c,   # red
    "finance_destroyed": 0xe74c3c,   # red
    "finance_lost": 0x95a5a6,        # gray
    "gf_installment_created": 0xf39c12,  # orange
    "gf_installment_paid_off": 0x2ecc71, # green
    "aashop_installment_created": 0x1abc9c,  # teal
    "aashop_installment_paid_off": 0x2ecc71,  # green
    "aashop_installment_defaulted": 0xe74c3c,  # red
}


def notify_admin_webhook(event_type, title, detail, member=None):
    """Post an event to the admin Discord webhook channel.

    This is for admin monitoring — a shared Discord channel where admins can
    see all plugin activity (rentals, returns, finances, defaults, etc.).

    Set SHIPFINANCE_ADMIN_DISCORD_WEBHOOK in local.py to a Discord channel
    webhook URL to enable. If not set or empty, this is a no-op.

    Args:
        event_type: one of the keys in _WEBHOOK_COLORS (controls embed color)
        title: short title for the embed
        detail: message body (string)
        member: optional User object, shown as "Member: <username>"
    """
    webhook_url = app_settings.SHIPFINANCE_ADMIN_DISCORD_WEBHOOK
    if not webhook_url:
        return
    try:
        color = _WEBHOOK_COLORS.get(event_type, 0x3498db)
        fields = [{"name": "Event", "value": event_type, "inline": True}]
        if member:
            fields.append({
                "name": "Member", "value": member.username, "inline": True})
        fields.append({"name": "Details", "value": detail[:1024], "inline": False})

        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": fields,
                "timestamp": timezone.now().isoformat(),
            }],
        }
        resp = requests.post(
            webhook_url, json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            logger.warning(
                f"Admin webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Admin webhook failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def mark_ship_returned(rental, performed_by=None):
    """Mark a rental as returned, ship back in corp hangar.

    For free-use rentals: compute the actual time used and create an
    invoice for the billed amount (rate * number of billing periods,
    rounded up). The invoice is created at return time, not upfront.
    """
    stock = rental.ship_stock
    stock.state = ShipStockState.AVAILABLE
    stock.save()

    now = timezone.now()
    rental.status = RentalStatus.RETURNED
    rental.return_detected_at = now
    rental.save()

    # For free-use rentals, bill for actual time used
    fee = Decimal("0")
    if rental.delivery_mode == DeliveryMode.FREE_USE and rental.invoice_id is None:
        duration = now - rental.start_time
        fee = compute_rental_fee(
            rental.rate, rental.billing_period, duration)
        if fee > 0 and rental.member_character:
            invoice = create_invoice(
                character=rental.member_character,
                amount=fee,
                due_date=now + timedelta(days=7),
                note=f"Free-use rental: {stock.doctrine_fit.name} "
                     f"({rental.billing_period} billing, "
                     f"{rental.duration_display})")
            if invoice:
                rental.invoice = invoice
                rental.save()
                log_action(
                    "FREE_USE_BILLED", performed_by=performed_by,
                    rental_agreement=rental, ship_stock=stock,
                    detail=f"Free-use rental {rental.id} billed {fee} ISK "
                           f"for {rental.duration_display}")

    log_action("SHIP_RETURNED", performed_by=performed_by,
               rental_agreement=rental, ship_stock=stock,
               detail=f"Rental {rental.id} returned")
    notify_member(
        rental.member, "Ship Returned",
        f"Your rental of {stock.doctrine_fit.name} has been marked returned. "
        + (f"You've been billed {fee} ISK for {rental.duration_display}."
           if rental.delivery_mode == DeliveryMode.FREE_USE and fee > 0
           else "Thank you!"),
        eve_character=rental.member_character)
    webhook_detail = f"Rental #{rental.id}: {stock.doctrine_fit.name} returned by {rental.member.username}"
    if rental.delivery_mode == DeliveryMode.FREE_USE and fee > 0:
        webhook_detail += f" — billed {fee} ISK for {rental.duration_display}"
    notify_admin_webhook(
        "rental_returned", "Rental Returned",
        webhook_detail,
        member=rental.member)


def mark_ship_destroyed(rental_or_finance, is_finance, performed_by=None,
                        killmail_url=None):
    """Mark a rented or financed ship as destroyed."""
    if is_finance:
        fa = rental_or_finance
        stock = fa.ship_stock
        # GeorgeForge and aa-shop installment plans have no physical stock;
        # "destroyed" doesn't apply — use "defaulted" instead.
        if stock is None:
            raise ValueError(
                f"Finance {fa.id} has no physical ship stock (GeorgeForge/aa-shop "
                "installment plan). Use mark_finance_defaulted instead.")
        stock.state = ShipStockState.DESTROYED
        stock.save()
        if fa.insurance_purchased and fa.insurance_coverage_amount > 0:
            # Settle remaining balance via insurance: void unpaid installments
            coverage = fa.insurance_coverage_amount
            remaining = fa.remaining_balance
            if coverage >= remaining:
                # Insurance covers full remaining balance; void unpaid invoices
                for inst in fa.installments.filter(invoice__paid=False):
                    inv = inst.invoice
                    inv.paid = True
                    inv.marked_paid_by = None
                    inv.note = (inv.note or "") + " [SETTLED BY INSURANCE]"
                    inv.save()
                fa.status = FinanceStatus.DESTROYED_INSURED
            else:
                # Partial coverage: mark what we can, member still owes the rest
                fa.status = FinanceStatus.DESTROYED_INSURED
                # Log the partial coverage detail to the audit log
                log_action(
                    "INSURANCE_PARTIAL_COVERAGE", finance_agreement=fa,
                    ship_stock=stock,
                    detail=f"Finance {fa.id} partial insurance: {coverage} of {remaining} ISK covered. "
                           f"Member still owes {remaining - coverage} ISK.")
        else:
            # No insurance: member still owes the full remaining balance
            fa.status = FinanceStatus.DESTROYED
        fa.save()
        log_action("SHIP_DESTROYED", performed_by=performed_by,
                   finance_agreement=fa, ship_stock=stock,
                   detail=f"Finance {fa.id} ship destroyed. Insurance: {fa.insurance_purchased}. "
                          f"Killmail: {killmail_url or 'n/a'}")
        notify_member(
            fa.member, "Financed Ship Destroyed",
            f"Your financed {stock.doctrine_fit.name} has been reported destroyed. "
            + ("Insurance is settling your remaining balance." if fa.insurance_purchased
               else "You still owe the remaining balance per your finance agreement."),
            eve_character=fa.member_character)
        notify_admin_webhook(
            "finance_destroyed", "Financed Ship Destroyed",
            f"Finance #{fa.id}: {stock.doctrine_fit.name} destroyed. "
            f"Insurance: {'yes' if fa.insurance_purchased else 'no'}. "
            f"Killmail: {killmail_url or 'n/a'}",
            member=fa.member)
    else:
        rental = rental_or_finance
        stock = rental.ship_stock
        stock.state = ShipStockState.DESTROYED
        stock.save()
        rental.status = RentalStatus.DESTROYED
        rental.save()
        log_action("SHIP_DESTROYED", performed_by=performed_by,
                   rental_agreement=rental, ship_stock=stock,
                   detail=f"Rental {rental.id} ship destroyed. Killmail: {killmail_url or 'n/a'}")
        notify_member(
            rental.member, "Rented Ship Destroyed",
            f"Your rented {stock.doctrine_fit.name} has been reported destroyed. "
            "Rental closed.",
            eve_character=rental.member_character)
        notify_admin_webhook(
            "rental_destroyed", "Rented Ship Destroyed",
            f"Rental #{rental.id}: {stock.doctrine_fit.name} destroyed. "
            f"Killmail: {killmail_url or 'n/a'}",
            member=rental.member)


def mark_ship_lost(rental_or_finance, is_finance, performed_by=None):
    """Mark a ship as lost/unaccounted (not returned, not on any killmail)."""
    if is_finance:
        fa = rental_or_finance
        stock = fa.ship_stock
        # GeorgeForge and aa-shop installment plans have no physical stock;
        # "lost" doesn't apply — use "defaulted" instead.
        if stock is None:
            raise ValueError(
                f"Finance {fa.id} has no physical ship stock (GeorgeForge/aa-shop "
                "installment plan). Use mark_finance_defaulted instead.")
        stock.state = ShipStockState.LOST
        stock.save()
        fa.status = FinanceStatus.DEFAULTED
        fa.save()
        log_action("SHIP_LOST", performed_by=performed_by,
                   finance_agreement=fa, ship_stock=stock,
                   detail=f"Finance {fa.id} ship lost/unaccounted. Admin review needed.")
        notify_admin_webhook(
            "finance_lost", "Financed Ship Lost",
            f"Finance #{fa.id}: {stock.doctrine_fit.name} lost/unaccounted. "
            f"Admin review needed.",
            member=fa.member)
    else:
        rental = rental_or_finance
        stock = rental.ship_stock
        stock.state = ShipStockState.LOST
        stock.save()
        rental.status = RentalStatus.LOST
        rental.save()
        log_action("SHIP_LOST", performed_by=performed_by,
                   rental_agreement=rental, ship_stock=stock,
                   detail=f"Rental {rental.id} ship lost/unaccounted. Admin review needed.")
        notify_admin_webhook(
            "rental_lost", "Rented Ship Lost",
            f"Rental #{rental.id}: {stock.doctrine_fit.name} lost/unaccounted. "
            f"Admin review needed.",
            member=rental.member)


def mark_finance_paid_off(fa, performed_by=None):
    """Mark a finance agreement as paid off (all installments settled)."""
    fa.status = FinanceStatus.PAID_OFF
    fa.paid_off_date = timezone.now()
    fa.save()

    if fa.is_georgeforge:
        # GeorgeForge installment plan: mark the GF order as ready to build
        from . import georgeforge_integration
        georgeforge_integration.mark_order_ready_to_build(fa.georgeforge_order_id)
        log_action("FINANCE_PAID_OFF", performed_by=performed_by,
                   finance_agreement=fa,
                   detail=f"Finance {fa.id} paid off. GF order #{fa.georgeforge_order_id} "
                          "marked as ready to build.")
        notify_member(
            fa.member, "Installment Plan Complete!",
            f"You've fully paid off your GeorgeForge order "
            f"({fa.georgeforge_item_name}). The ship will now be built. "
            f"Watch for delivery from GeorgeForge.",
            eve_character=fa.member_character)
        notify_admin_webhook(
            "gf_installment_paid_off", "GF Installment Plan Paid Off",
            f"Finance #{fa.id}: GeorgeForge order #{fa.georgeforge_order_id} "
            f"({fa.georgeforge_item_name}) fully paid off. Ship ready to build.",
            member=fa.member)
    elif fa.is_aashop:
        # aa-shop installment plan: notify shop owner to proceed with contract
        log_action("FINANCE_PAID_OFF", performed_by=performed_by,
                   finance_agreement=fa,
                   detail=f"Finance {fa.id} paid off. aa-shop order #{fa.aashop_order_reference} "
                          "ready to contract.")
        notify_member(
            fa.member, "Installment Plan Complete!",
            f"You've fully paid off your aa-shop order "
            f"({fa.aashop_item_summary}, order #{fa.aashop_order_reference}). "
            f"Contact the shop owner to arrange delivery.",
            eve_character=fa.member_character)
        notify_admin_webhook(
            "aashop_installment_paid_off", "Shop Order Paid Off — Ready to Contract",
            f"Finance #{fa.id}: aa-shop order #{fa.aashop_order_reference} "
            f"({fa.aashop_item_summary}) fully paid off by {fa.member.username}. "
            f"The shop owner can now accept the order and contract the items.",
            member=fa.member)
    else:
        stock = fa.ship_stock
        if stock:
            stock.state = ShipStockState.SOLD
            stock.save()
        log_action("FINANCE_PAID_OFF", performed_by=performed_by,
                   finance_agreement=fa, ship_stock=stock,
                   detail=f"Finance {fa.id} paid off. Ship now owned by member.")
        notify_member(
            fa.member, "Finance Complete!",
            f"Congratulations! You've paid off your {stock.doctrine_fit.name if stock else 'ship'}. "
            "The ship is now yours.",
            eve_character=fa.member_character)
        notify_admin_webhook(
            "finance_paid_off", "Finance Paid Off",
            f"Finance #{fa.id}: {stock.doctrine_fit.name if stock else 'ship'} "
            f"paid off by {fa.member.username}. Ship now owned by member.",
            member=fa.member)


def mark_finance_defaulted(fa, performed_by=None, detail=""):
    """Admin action: mark a finance as defaulted (member stopped paying)."""
    fa.status = FinanceStatus.DEFAULTED
    fa.save()
    log_action("FINANCE_DEFAULTED", performed_by=performed_by,
               finance_agreement=fa, detail=detail or f"Finance {fa.id} defaulted by admin")
    item_name = fa.item_display_name
    notify_member(
        fa.member, "Finance Defaulted",
        f"Your finance agreement for {item_name} has been "
        "marked as defaulted. Please contact a director.",
        eve_character=fa.member_character)
    notify_admin_webhook(
        "finance_defaulted", "Finance Defaulted",
        f"Finance #{fa.id}: {item_name} defaulted. {detail or 'Marked by admin.'}",
        member=fa.member)
    # For aa-shop installment plans, notify the shop owner that the order
    # can be declined/cancelled since the buyer has defaulted.
    if fa.is_aashop:
        notify_admin_webhook(
            "aashop_installment_defaulted", "Shop Order Installment Defaulted",
            f"Finance #{fa.id}: aa-shop order #{fa.aashop_order_reference} "
            f"({fa.aashop_item_summary}) defaulted by {fa.member.username}. "
            f"The shop owner can now decline/cancel this order.",
            member=fa.member)
