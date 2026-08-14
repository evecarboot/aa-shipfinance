"""Views for shipfinance: member browse/rent/finance, admin manage."""
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import app_settings, helpers
from .georgeforge_integration import create_delivery_contract
from .models import (
    BillingPeriod,
    DeliveryMode,
    DoctrineFit,
    FinanceAgreement,
    FinanceOffer,
    FinanceStatus,
    FreeUseHangar,
    InsuranceCoverage,
    InterestType,
    RentalAgreement,
    RentalStatus,
    ShipStock,
    ShipStockState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Member views
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.access_shipfinance")
def index(request):
    """Landing page: summary of available ships, member's active agreements."""
    fits = DoctrineFit.objects.filter(active=True).order_by("skill_tier", "name")
    my_rentals = RentalAgreement.objects.visible_to(request.user).filter(
        status__in=[RentalStatus.ACTIVE, RentalStatus.OVERDUE])[:5]
    my_finances = FinanceAgreement.objects.visible_to(request.user).filter(
        status=FinanceStatus.ACTIVE)[:5]
    ctx = {
        "fits": fits,
        "my_rentals": my_rentals,
        "my_finances": my_finances,
        "can_rent": request.user.has_perm("shipfinance.use_rent"),
        "can_finance": request.user.has_perm("shipfinance.use_finance"),
        "can_manage": request.user.has_perm("shipfinance.manage_shipfinance"),
    }
    return render(request, "shipfinance/index.html", ctx)


@login_required
@permission_required("shipfinance.access_shipfinance")
def browse_ships(request):
    """Browse available doctrine fits and their finance offers."""
    fits = DoctrineFit.objects.filter(active=True).order_by("skill_tier", "name")
    offers = FinanceOffer.objects.filter(active=True).select_related("doctrine_fit")
    ctx = {
        "fits": fits,
        "offers": offers,
        "can_rent": request.user.has_perm("shipfinance.use_rent"),
        "can_finance": request.user.has_perm("shipfinance.use_finance"),
    }
    return render(request, "shipfinance/browse.html", ctx)


@login_required
@permission_required("shipfinance.use_rent")
def rent_ship(request, fit_id):
    """Rent a ship: member picks duration, acknowledges terms.

    Rates are set by the admin on the doctrine fit. The member just picks
    a billing period (from the ones the admin has priced) and a duration.

    This is for contract/hangar_request rentals only. Free-use rentals
    are auto-detected by the detect_free_use_taken task when a member
    takes a ship from a free-use hangar — no form needed.
    """
    fit = get_object_or_404(DoctrineFit, pk=fit_id, active=True)
    available_stock = ShipStock.objects.filter(
        doctrine_fit=fit, state=ShipStockState.AVAILABLE)

    if not available_stock.exists():
        messages.error(request, f"No {fit.name} currently available for rent.")
        return redirect("shipfinance:browse")

    if not app_settings.invoices_installed():
        messages.error(request, "The invoices plugin is required for rentals.")
        return redirect("shipfinance:browse")

    # Get the rental options the admin has configured for this fit
    rental_options = fit.rental_options
    if not rental_options:
        messages.error(request, f"No rental rates configured for {fit.name}. Ask an admin.")
        return redirect("shipfinance:browse")

    if request.method == "POST":
        delivery_mode = request.POST.get("delivery_mode", DeliveryMode.CONTRACT)
        billing_period = request.POST.get("billing_period")
        acknowledge = request.POST.get("acknowledge") == "on"

        # Free-use is auto-detected, not a form choice
        if delivery_mode == DeliveryMode.FREE_USE:
            messages.error(request, "Free-use rentals are automatic — just take a ship from the free-use hangar.")
            return redirect("shipfinance:rent_ship", fit_id=fit.id)

        if not acknowledge:
            messages.error(request, "You must acknowledge the terms to rent.")
            return redirect("shipfinance:rent_ship", fit_id=fit.id)

        # Look up the rate from the admin-configured options
        rate = None
        for bp, r in rental_options:
            if bp == billing_period:
                rate = r
                break
        if rate is None:
            messages.error(request, "Invalid billing period for this fit.")
            return redirect("shipfinance:rent_ship", fit_id=fit.id)

        try:
            duration_hours = int(request.POST.get("duration_hours", 24))
        except ValueError:
            messages.error(request, "Invalid duration value.")
            return redirect("shipfinance:rent_ship", fit_id=fit.id)

        if duration_hours <= 0:
            messages.error(request, "Duration must be at least 1 hour.")
            return redirect("shipfinance:rent_ship", fit_id=fit.id)

        # Pick the first available stock
        stock = available_stock.first()
        now = timezone.now()
        due = now + timedelta(hours=duration_hours)

        # Compute rental fee from admin-set rate
        duration = due - now
        fee = helpers.compute_rental_fee(rate, billing_period, duration)

        # Get member's main character
        char = _get_main_character(request.user)
        if char is None:
            messages.error(request, "You need a main character set to rent a ship.")
            return redirect("shipfinance:rent_ship", fit_id=fit.id)

        # Create rental fee invoice
        invoice = helpers.create_invoice(
            character=char, amount=fee, due_date=due,
            note=f"Rental: {fit.name} ({delivery_mode}, {duration_hours}h)")

        # Create rental agreement
        terms = (f"Rental of {fit.name} for {duration_hours} hours. "
                 f"Fee: {fee} ISK. Return the ship to corp hangar division "
                 f"{stock.hangar_division} at {stock.location_name} by {due}. "
                 f"No refunds. Ship is tracked by ESI asset ID.")
        rental = RentalAgreement.objects.create(
            ship_stock=stock,
            member=request.user,
            member_character=char,
            delivery_mode=delivery_mode,
            start_time=now,
            due_date=due,
            billing_period=billing_period,
            rate=rate,
            invoice=invoice,
            status=RentalStatus.ACTIVE,
            terms_acknowledged=True,
            terms_text=terms,
            acknowledged_at=now,
        )

        # Mark stock out
        stock.state = ShipStockState.OUT_RENT
        stock.save()

        helpers.log_action(
            "RENTAL_CREATED", performed_by=request.user,
            rental_agreement=rental, ship_stock=stock,
            detail=f"Rental {rental.id} created for {fit.name}")

        # Try GeorgeForge delivery if applicable
        if delivery_mode == DeliveryMode.CONTRACT and app_settings.georgeforge_installed():
            create_delivery_contract(stock, char, note=f"Rental {rental.id}")

        messages.success(
            request,
            f"Rental created! Ship: {fit.name}. Fee: {fee} ISK. "
            f"Due back by {due:%Y-%m-%d %H:%M}. "
            f"Pay invoice ref: {invoice.invoice_ref if invoice else 'N/A'}")
        return redirect("shipfinance:my_rentals")

    # GET: show rental form with admin-configured rates
    ctx = {
        "fit": fit,
        "available_count": available_stock.count(),
        "rental_options": rental_options,
    }
    return render(request, "shipfinance/rent/rent_form.html", ctx)


@login_required
@permission_required("shipfinance.use_finance")
def finance_ship(request, offer_id):
    """Finance a ship: member accepts an offer, gets ship up front, pays installments."""
    offer = get_object_or_404(FinanceOffer, pk=offer_id, active=True)
    available_stock = ShipStock.objects.filter(
        doctrine_fit=offer.doctrine_fit, state=ShipStockState.AVAILABLE)

    if not available_stock.exists():
        messages.error(request, f"No {offer.doctrine_fit.name} currently in stock to finance.")
        return redirect("shipfinance:browse")

    if not app_settings.invoices_installed():
        messages.error(request, "The invoices plugin is required for finance.")
        return redirect("shipfinance:browse")

    # Compute schedule for display
    total, monthly = helpers.compute_finance_schedule(
        offer.principal, offer.term_months, offer.interest_type, offer.interest_rate)
    insurance_premium = helpers.compute_insurance_premium(
        offer.principal, offer.insurance_premium_rate) if offer.insurance_enabled else Decimal("0")

    if request.method == "POST":
        buy_insurance = offer.insurance_enabled and request.POST.get("buy_insurance") == "on"
        acknowledge = request.POST.get("acknowledge") == "on"

        if not acknowledge:
            messages.error(request, "You must acknowledge the terms to finance a ship.")
            return redirect("shipfinance:finance_ship", offer_id=offer.id)

        char = _get_main_character(request.user)
        if char is None:
            messages.error(request, "You need a main character set to finance a ship.")
            return redirect("shipfinance:finance_ship", offer_id=offer.id)

        stock = available_stock.first()
        now = timezone.now()

        # Create finance agreement with snapshot of terms
        fa = FinanceAgreement.objects.create(
            finance_offer=offer,
            ship_stock=stock,
            member=request.user,
            member_character=char,
            principal=offer.principal,
            term_months=offer.term_months,
            interest_type=offer.interest_type,
            interest_rate=offer.interest_rate,
            total_amount=total,
            monthly_payment=monthly,
            insurance_purchased=buy_insurance,
            insurance_premium=insurance_premium if buy_insurance else Decimal("0"),
            insurance_coverage=offer.insurance_coverage,
            insurance_flat_amount=offer.insurance_flat_amount,
            status=FinanceStatus.ACTIVE,
            terms_acknowledged=True,
            terms_text=offer.terms_text,
            acknowledged_at=now,
        )

        # Mark stock out
        stock.state = ShipStockState.OUT_FINANCE
        stock.save()

        # Create installment invoices
        installments = helpers.build_installment_schedule(fa)

        # Create insurance invoice if purchased
        if buy_insurance:
            helpers.create_insurance_invoice(fa)

        helpers.log_action(
            "FINANCE_CREATED", performed_by=request.user,
            finance_agreement=fa, ship_stock=stock,
            detail=f"Finance {fa.id} created for {offer.doctrine_fit.name}, "
                   f"{offer.term_months} months, insurance={buy_insurance}")

        # Try GeorgeForge delivery
        if app_settings.georgeforge_installed():
            create_delivery_contract(stock, char, note=f"Finance {fa.id}")

        messages.success(
            request,
            f"Finance agreement created! Ship: {offer.doctrine_fit.name}. "
            f"Monthly: {monthly} ISK for {offer.term_months} months. "
            f"Total: {total} ISK. "
            + (f"Insurance premium: {insurance_premium} ISK." if buy_insurance else ""))
        return redirect("shipfinance:my_finances")

    ctx = {
        "offer": offer,
        "available_count": available_stock.count(),
        "total_amount": total,
        "monthly_payment": monthly,
        "insurance_premium": insurance_premium,
        "insurance_coverage_display": _coverage_display(offer),
    }
    return render(request, "shipfinance/finance/finance_form.html", ctx)


@login_required
@permission_required("shipfinance.access_shipfinance")
def my_rentals(request):
    """List the member's rental agreements."""
    rentals = RentalAgreement.objects.visible_to(request.user).order_by("-created_at")
    ctx = {"rentals": rentals}
    return render(request, "shipfinance/rent/my_rentals.html", ctx)


@login_required
@permission_required("shipfinance.access_shipfinance")
def my_finances(request):
    """List the member's finance agreements."""
    finances = FinanceAgreement.objects.visible_to(request.user).order_by("-created_at")
    ctx = {"finances": finances}
    return render(request, "shipfinance/finance/my_finances.html", ctx)


# ---------------------------------------------------------------------------
# Admin: dashboard
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_dashboard(request):
    """Admin overview: counts of stock, agreements, overdue."""
    ctx = {
        "fits_count": DoctrineFit.objects.count(),
        "stock_count": ShipStock.objects.count(),
        "available_count": ShipStock.objects.filter(state=ShipStockState.AVAILABLE).count(),
        "active_rentals": RentalAgreement.objects.filter(status=RentalStatus.ACTIVE).count(),
        "overdue_rentals": RentalAgreement.objects.filter(status=RentalStatus.OVERDUE).count(),
        "active_finances": FinanceAgreement.objects.filter(status=FinanceStatus.ACTIVE).count(),
        "offers_count": FinanceOffer.objects.count(),
        "georgeforge_available": app_settings.georgeforge_installed(),
        "invoices_available": app_settings.invoices_installed(),
    }
    return render(request, "shipfinance/admin/dashboard.html", ctx)


# ---------------------------------------------------------------------------
# Admin: doctrine fits
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_fits(request):
    fits = DoctrineFit.objects.all().order_by("-active", "name")
    ctx = {"fits": fits}
    return render(request, "shipfinance/admin/fits.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_fit_edit(request, fit_id=None):
    """Create or edit a doctrine fit."""
    fit = get_object_or_404(DoctrineFit, pk=fit_id) if fit_id else None
    if request.method == "POST":
        name = request.POST.get("name", "")
        hull_type_name = request.POST.get("hull_type_name", "")
        dna = request.POST.get("dna", "")
        skill_tier = request.POST.get("skill_tier", "")
        description = request.POST.get("description", "")
        active = request.POST.get("active") == "on"
        free_use_billing_period = request.POST.get(
            "free_use_billing_period", app_settings.SHIPFINANCE_DEFAULT_BILLING_PERIOD)

        try:
            hull_type_id = int(request.POST.get("hull_type_id", 0))
            free_use_rate = Decimal(request.POST.get("free_use_rate", "0"))
            rent_rate_hourly = Decimal(request.POST.get("rent_rate_hourly", "0"))
            rent_rate_daily = Decimal(request.POST.get("rent_rate_daily", "0"))
            rent_rate_weekly = Decimal(request.POST.get("rent_rate_weekly", "0"))
        except (ValueError, InvalidOperation):
            messages.error(request, "Hull type ID and rate fields must be numbers.")
            return redirect(request.path)

        if not name or not hull_type_id:
            messages.error(request, "Name and hull type ID are required.")
            return redirect(request.path)

        if fit:
            fit.name = name
            fit.hull_type_id = hull_type_id
            fit.hull_type_name = hull_type_name
            fit.dna = dna
            fit.skill_tier = skill_tier
            fit.description = description
            fit.active = active
            fit.free_use_rate = free_use_rate
            fit.free_use_billing_period = free_use_billing_period
            fit.rent_rate_hourly = rent_rate_hourly
            fit.rent_rate_daily = rent_rate_daily
            fit.rent_rate_weekly = rent_rate_weekly
            fit.save()
            messages.success(request, f"Updated fit: {fit.name}")
        else:
            fit = DoctrineFit.objects.create(
                name=name, hull_type_id=hull_type_id, hull_type_name=hull_type_name,
                dna=dna, skill_tier=skill_tier, description=description, active=active,
                free_use_rate=free_use_rate, free_use_billing_period=free_use_billing_period,
                rent_rate_hourly=rent_rate_hourly, rent_rate_daily=rent_rate_daily,
                rent_rate_weekly=rent_rate_weekly)
            messages.success(request, f"Created fit: {fit.name}")
        return redirect("shipfinance:admin_fits")

    ctx = {"fit": fit, "billing_periods": BillingPeriod.CHOICES}
    return render(request, "shipfinance/admin/fit_edit.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_fit_delete(request, fit_id):
    fit = get_object_or_404(DoctrineFit, pk=fit_id)
    if request.method == "POST":
        fit.delete()
        messages.success(request, f"Deleted fit: {fit.name}")
    return redirect("shipfinance:admin_fits")


# ---------------------------------------------------------------------------
# Admin: stock
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_stock(request):
    stock = ShipStock.objects.all().select_related("doctrine_fit").order_by("-created_at")
    ctx = {"stock": stock}
    return render(request, "shipfinance/admin/stock.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_stock_edit(request, stock_id=None):
    """Register or edit a ship stock item."""
    stock = get_object_or_404(ShipStock, pk=stock_id) if stock_id else None
    fits = DoctrineFit.objects.filter(active=True).order_by("name")
    if request.method == "POST":
        fit_id = request.POST.get("doctrine_fit")
        item_id = request.POST.get("item_id", "").strip()
        item_name = request.POST.get("item_name", "")
        location_name = request.POST.get("location_name", "")
        state = request.POST.get("state", ShipStockState.AVAILABLE)

        if not fit_id or not item_id:
            messages.error(request, "Doctrine fit and ESI item ID are required.")
            return redirect(request.path)

        fit = get_object_or_404(DoctrineFit, pk=fit_id)
        try:
            item_id_int = int(item_id)
            location_id_int = int(request.POST.get("location_id", "0"))
            hangar_division = int(request.POST.get("hangar_division", 1))
        except ValueError:
            messages.error(request, "Item ID, location ID, and hangar division must be numbers.")
            return redirect(request.path)

        if stock:
            stock.doctrine_fit = fit
            stock.item_id = item_id_int
            stock.item_name = item_name
            stock.location_id = location_id_int
            stock.location_name = location_name
            stock.hangar_division = hangar_division
            stock.state = state
            stock.save()
            messages.success(request, f"Updated stock: {stock}")
        else:
            stock = ShipStock.objects.create(
                doctrine_fit=fit, item_id=item_id_int, item_name=item_name,
                location_id=location_id_int, location_name=location_name,
                hangar_division=hangar_division, state=state,
                registered_by=request.user)
            messages.success(request, f"Registered stock: {stock}")
        return redirect("shipfinance:admin_stock")

    ctx = {"stock": stock, "fits": fits, "states": ShipStockState.CHOICES}
    return render(request, "shipfinance/admin/stock_edit.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_stock_delete(request, stock_id):
    stock = get_object_or_404(ShipStock, pk=stock_id)
    if request.method == "POST":
        stock.delete()
        messages.success(request, f"Deleted stock: {stock}")
    return redirect("shipfinance:admin_stock")


# ---------------------------------------------------------------------------
# Admin: free-use hangars
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_hangars(request):
    hangars = FreeUseHangar.objects.all().order_by("location_name", "hangar_division")
    ctx = {"hangars": hangars}
    return render(request, "shipfinance/admin/hangars.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_hangar_edit(request, hangar_id=None):
    hangar = get_object_or_404(FreeUseHangar, pk=hangar_id) if hangar_id else None
    if request.method == "POST":
        location_name = request.POST.get("location_name", "")
        active = request.POST.get("active") == "on"

        try:
            location_id = int(request.POST.get("location_id", 0))
            hangar_division = int(request.POST.get("hangar_division", 1))
        except ValueError:
            messages.error(request, "Location ID and hangar division must be numbers.")
            return redirect(request.path)

        if not location_id:
            messages.error(request, "Location ID is required.")
            return redirect(request.path)

        if hangar_division < 1 or hangar_division > 7:
            messages.error(request, "Hangar division must be 1-7.")
            return redirect(request.path)

        if hangar:
            hangar.location_id = location_id
            hangar.location_name = location_name
            hangar.hangar_division = hangar_division
            hangar.active = active
            hangar.save()
            messages.success(request, f"Updated hangar: {hangar}")
        else:
            hangar = FreeUseHangar.objects.create(
                location_id=location_id, location_name=location_name,
                hangar_division=hangar_division, active=active)
            messages.success(request, f"Created hangar: {hangar}")
        return redirect("shipfinance:admin_hangars")

    ctx = {"hangar": hangar}
    return render(request, "shipfinance/admin/hangar_edit.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_hangar_delete(request, hangar_id):
    hangar = get_object_or_404(FreeUseHangar, pk=hangar_id)
    if request.method == "POST":
        hangar.delete()
        messages.success(request, f"Deleted hangar: {hangar}")
    return redirect("shipfinance:admin_hangars")


# ---------------------------------------------------------------------------
# Admin: finance offers
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_offers(request):
    offers = FinanceOffer.objects.all().select_related("doctrine_fit").order_by("-active", "-created_at")
    ctx = {"offers": offers}
    return render(request, "shipfinance/admin/offers.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_offer_edit(request, offer_id=None):
    offer = get_object_or_404(FinanceOffer, pk=offer_id) if offer_id else None
    fits = DoctrineFit.objects.filter(active=True).order_by("name")
    if request.method == "POST":
        fit_id = request.POST.get("doctrine_fit")
        name = request.POST.get("name", "")
        interest_type = request.POST.get("interest_type", InterestType.FLAT)
        insurance_coverage = request.POST.get(
            "insurance_coverage", app_settings.SHIPFINANCE_DEFAULT_INSURANCE_COVERAGE)
        terms_text = request.POST.get("terms_text", "")
        active = request.POST.get("active") == "on"
        insurance_enabled = request.POST.get("insurance_enabled") == "on"

        try:
            principal = Decimal(request.POST.get("principal", "0"))
            term_months = int(request.POST.get("term_months", 3))
            interest_rate = Decimal(request.POST.get("interest_rate", "10"))
            insurance_premium_rate = Decimal(request.POST.get("insurance_premium_rate", "5"))
            insurance_flat_amount = Decimal(request.POST.get("insurance_flat_amount", "0"))
        except (ValueError, InvalidOperation):
            messages.error(request, "Invalid numeric value in form.")
            return redirect(request.path)

        if not fit_id or not name or principal <= 0:
            messages.error(request, "Fit, name, and a positive principal are required.")
            return redirect(request.path)

        fit = get_object_or_404(DoctrineFit, pk=fit_id)
        if offer:
            offer.doctrine_fit = fit
            offer.name = name
            offer.principal = principal
            offer.term_months = term_months
            offer.interest_type = interest_type
            offer.interest_rate = interest_rate
            offer.insurance_enabled = insurance_enabled
            offer.insurance_premium_rate = insurance_premium_rate
            offer.insurance_coverage = insurance_coverage
            offer.insurance_flat_amount = insurance_flat_amount
            offer.terms_text = terms_text
            offer.active = active
            offer.save()
            messages.success(request, f"Updated offer: {offer.name}")
        else:
            offer = FinanceOffer.objects.create(
                doctrine_fit=fit, name=name, principal=principal,
                term_months=term_months, interest_type=interest_type,
                interest_rate=interest_rate, insurance_enabled=insurance_enabled,
                insurance_premium_rate=insurance_premium_rate,
                insurance_coverage=insurance_coverage,
                insurance_flat_amount=insurance_flat_amount,
                terms_text=terms_text, active=active)
            messages.success(request, f"Created offer: {offer.name}")
        return redirect("shipfinance:admin_offers")

    ctx = {
        "offer": offer, "fits": fits,
        "interest_types": InterestType.CHOICES,
        "coverage_choices": InsuranceCoverage.CHOICES,
    }
    return render(request, "shipfinance/admin/offer_edit.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_offer_delete(request, offer_id):
    offer = get_object_or_404(FinanceOffer, pk=offer_id)
    if request.method == "POST":
        offer.delete()
        messages.success(request, f"Deleted offer: {offer.name}")
    return redirect("shipfinance:admin_offers")


# ---------------------------------------------------------------------------
# Admin: agreements
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_rentals(request):
    rentals = RentalAgreement.objects.all().select_related(
        "ship_stock__doctrine_fit", "member").order_by("-created_at")
    ctx = {"rentals": rentals}
    return render(request, "shipfinance/admin/rentals.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_finances(request):
    finances = FinanceAgreement.objects.all().select_related(
        "finance_offer__doctrine_fit", "ship_stock__doctrine_fit", "member").order_by("-created_at")
    ctx = {"finances": finances}
    return render(request, "shipfinance/admin/finances.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_mark_returned(request, rental_id):
    rental = get_object_or_404(RentalAgreement, pk=rental_id)
    if request.method == "POST":
        helpers.mark_ship_returned(rental, performed_by=request.user)
        messages.success(request, f"Marked rental {rental.id} as returned.")
    return redirect("shipfinance:admin_rentals")


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_mark_destroyed_rental(request, rental_id):
    rental = get_object_or_404(RentalAgreement, pk=rental_id)
    if request.method == "POST":
        killmail_url = request.POST.get("killmail_url", "")
        helpers.mark_ship_destroyed(rental, is_finance=False,
                                    performed_by=request.user, killmail_url=killmail_url)
        messages.success(request, f"Marked rental {rental.id} ship as destroyed.")
    ctx = {"rental": rental}
    return render(request, "shipfinance/admin/mark_destroyed.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_mark_lost_rental(request, rental_id):
    rental = get_object_or_404(RentalAgreement, pk=rental_id)
    if request.method == "POST":
        helpers.mark_ship_lost(rental, is_finance=False, performed_by=request.user)
        messages.success(request, f"Marked rental {rental.id} ship as lost.")
    return redirect("shipfinance:admin_rentals")


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_mark_paid_off(request, finance_id):
    fa = get_object_or_404(FinanceAgreement, pk=finance_id)
    if request.method == "POST":
        helpers.mark_finance_paid_off(fa, performed_by=request.user)
        messages.success(request, f"Marked finance {fa.id} as paid off.")
    return redirect("shipfinance:admin_finances")


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_mark_defaulted(request, finance_id):
    fa = get_object_or_404(FinanceAgreement, pk=finance_id)
    if request.method == "POST":
        detail = request.POST.get("detail", "")
        helpers.mark_finance_defaulted(fa, performed_by=request.user, detail=detail)
        messages.success(request, f"Marked finance {fa.id} as defaulted.")
    return redirect("shipfinance:admin_finances")


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_mark_destroyed_finance(request, finance_id):
    fa = get_object_or_404(FinanceAgreement, pk=finance_id)
    if request.method == "POST":
        killmail_url = request.POST.get("killmail_url", "")
        helpers.mark_ship_destroyed(fa, is_finance=True,
                                    performed_by=request.user, killmail_url=killmail_url)
        messages.success(request, f"Marked finance {fa.id} ship as destroyed.")
    ctx = {"finance": fa}
    return render(request, "shipfinance/admin/mark_destroyed.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_mark_lost_finance(request, finance_id):
    fa = get_object_or_404(FinanceAgreement, pk=finance_id)
    if request.method == "POST":
        helpers.mark_ship_lost(fa, is_finance=True, performed_by=request.user)
        messages.success(request, f"Marked finance {fa.id} ship as lost.")
    return redirect("shipfinance:admin_finances")


# ---------------------------------------------------------------------------
# Admin: audit log & setup
# ---------------------------------------------------------------------------

@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_audit(request):
    entries = AuditLog.objects.all().select_related(
        "performed_by", "ship_stock", "rental_agreement", "finance_agreement")[:200]
    ctx = {"entries": entries}
    return render(request, "shipfinance/admin/audit.html", ctx)


@login_required
@permission_required("shipfinance.manage_shipfinance")
def admin_setup_tasks(request):
    from .tasks import setup_default_tasks
    setup_default_tasks.apply_async()
    messages.success(request, "Default periodic tasks created/updated.")
    return redirect("shipfinance:admin_dashboard")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_main_character(user):
    """Get the user's main character, or None."""
    try:
        return user.profile.main_character
    except Exception:
        return None


def _coverage_display(offer):
    """Human-readable insurance coverage description."""
    from .models import InsuranceCoverage
    if offer.insurance_coverage == InsuranceCoverage.REMAINING_BALANCE:
        return "Remaining balance at time of loss"
    elif offer.insurance_coverage == InsuranceCoverage.PRINCIPAL:
        return f"Original principal ({offer.principal} ISK)"
    else:
        return f"Flat amount ({offer.insurance_flat_amount} ISK)"
