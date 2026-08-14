"""Helper logic: interest calculation, insurance, invoice creation, notifications."""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

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


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def mark_ship_returned(rental, performed_by=None):
    """Mark a rental as returned, ship back in corp hangar."""
    stock = rental.ship_stock
    stock.state = ShipStockState.AVAILABLE
    stock.save()
    rental.status = RentalStatus.RETURNED
    rental.return_detected_at = timezone.now()
    rental.save()
    log_action("SHIP_RETURNED", performed_by=performed_by,
               rental_agreement=rental, ship_stock=stock,
               detail=f"Rental {rental.id} returned")
    notify_member(
        rental.member, "Ship Returned",
        f"Your rental of {stock.doctrine_fit.name} has been marked returned. Thank you!",
        eve_character=rental.member_character)


def mark_ship_destroyed(rental_or_finance, is_finance, performed_by=None,
                        killmail_url=None):
    """Mark a rented or financed ship as destroyed."""
    if is_finance:
        fa = rental_or_finance
        stock = fa.ship_stock
        stock.state = ShipStockState.DESTROYED
        stock.save()
        if fa.insurance_purchased and fa.insurance_coverage_amount > 0:
            fa.status = FinanceStatus.DESTROYED_INSURED
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
                fa.notes = (fa.notes or "") + f"\nPartial insurance coverage: {coverage} of {remaining}"
                fa.save()
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


def mark_ship_lost(rental_or_finance, is_finance, performed_by=None):
    """Mark a ship as lost/unaccounted (not returned, not on any killmail)."""
    if is_finance:
        fa = rental_or_finance
        stock = fa.ship_stock
        stock.state = ShipStockState.LOST
        stock.save()
        fa.status = FinanceStatus.DEFAULTED
        fa.save()
        log_action("SHIP_LOST", performed_by=performed_by,
                   finance_agreement=fa, ship_stock=stock,
                   detail=f"Finance {fa.id} ship lost/unaccounted. Admin review needed.")
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


def mark_finance_paid_off(fa, performed_by=None):
    """Mark a finance agreement as paid off (all installments settled)."""
    stock = fa.ship_stock
    stock.state = ShipStockState.SOLD
    stock.save()
    fa.status = FinanceStatus.PAID_OFF
    fa.paid_off_date = timezone.now()
    fa.save()
    log_action("FINANCE_PAID_OFF", performed_by=performed_by,
               finance_agreement=fa, ship_stock=stock,
               detail=f"Finance {fa.id} paid off. Ship now owned by member.")
    notify_member(
        fa.member, "Finance Complete!",
        f"Congratulations! You've paid off your {stock.doctrine_fit.name}. "
        "The ship is now yours.",
        eve_character=fa.member_character)


def mark_finance_defaulted(fa, performed_by=None, detail=""):
    """Admin action: mark a finance as defaulted (member stopped paying)."""
    fa.status = FinanceStatus.DEFAULTED
    fa.save()
    log_action("FINANCE_DEFAULTED", performed_by=performed_by,
               finance_agreement=fa, detail=detail or f"Finance {fa.id} defaulted by admin")
    notify_member(
        fa.member, "Finance Defaulted",
        f"Your finance agreement for {fa.ship_stock.doctrine_fit.name} has been "
        "marked as defaulted. Please contact a director.",
        eve_character=fa.member_character)
