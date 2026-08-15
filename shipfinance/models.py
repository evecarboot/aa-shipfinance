"""Models for the shipfinance plugin."""
import logging
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from allianceauth.eveonline.models import EveCharacter

from . import app_settings
from .managers import FinanceAgreementManager, RentalAgreementManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class DeliveryMode:
    CONTRACT = "contract"
    HANGAR_REQUEST = "hangar_request"
    FREE_USE = "free_use"

    CHOICES = [
        (CONTRACT, "Contract to member"),
        (HANGAR_REQUEST, "Hangar access (request)"),
        (FREE_USE, "Self-service rental hangar"),
    ]


class BillingPeriod:
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"

    CHOICES = [
        (HOURLY, "Per hour"),
        (DAILY, "Per day"),
        (WEEKLY, "Per week"),
    ]

    @classmethod
    def to_timedelta(cls, period):
        from datetime import timedelta
        return {
            cls.HOURLY: timedelta(hours=1),
            cls.DAILY: timedelta(days=1),
            cls.WEEKLY: timedelta(days=7),
        }.get(period, timedelta(days=1))


class InterestType:
    FLAT = "flat"
    APR = "apr"

    CHOICES = [
        (FLAT, "Flat add-on"),
        (APR, "APR (declining balance)"),
    ]


class InsuranceCoverage:
    REMAINING_BALANCE = "remaining_balance"
    PRINCIPAL = "principal"
    FLAT_AMOUNT = "flat_amount"

    CHOICES = [
        (REMAINING_BALANCE, "Remaining balance at time of loss"),
        (PRINCIPAL, "Original principal"),
        (FLAT_AMOUNT, "Flat amount"),
    ]


class ShipStockState:
    AVAILABLE = "available"
    OUT_RENT = "out_rent"
    OUT_FINANCE = "out_finance"
    SOLD = "sold"
    LOST = "lost"
    DESTROYED = "destroyed"

    CHOICES = [
        (AVAILABLE, "Available"),
        (OUT_RENT, "Out on rental"),
        (OUT_FINANCE, "Out on finance"),
        (SOLD, "Sold (finance paid off)"),
        (LOST, "Lost (unaccounted)"),
        (DESTROYED, "Destroyed"),
    ]


class RentalStatus:
    ACTIVE = "active"
    RETURNED = "returned"
    OVERDUE = "overdue"
    DESTROYED = "destroyed"
    LOST = "lost"
    CLOSED = "closed"

    CHOICES = [
        (ACTIVE, "Active"),
        (RETURNED, "Returned"),
        (OVERDUE, "Overdue"),
        (DESTROYED, "Destroyed"),
        (LOST, "Lost (unaccounted)"),
        (CLOSED, "Closed"),
    ]


class FinanceStatus:
    ACTIVE = "active"
    PAID_OFF = "paid_off"
    DEFAULTED = "defaulted"
    DESTROYED = "destroyed"
    DESTROYED_INSURED = "destroyed_insured"
    CLOSED = "closed"

    CHOICES = [
        (ACTIVE, "Active"),
        (PAID_OFF, "Paid off"),
        (DEFAULTED, "Defaulted"),
        (DESTROYED, "Destroyed (no insurance)"),
        (DESTROYED_INSURED, "Destroyed (insured)"),
        (CLOSED, "Closed"),
    ]


# ---------------------------------------------------------------------------
# Catalogue & stock
# ---------------------------------------------------------------------------

class DoctrineFit(models.Model):
    """A doctrine ship fitting the corp is willing to rent or finance.

    Members browse by skill tier to pick a fit their skills support.
    The fitting is stored as a DNA string so it can be re-imported in-game.
    """

    name = models.CharField(max_length=100, help_text="Display name, e.g. 'Gila - T2'")
    hull_type_id = models.PositiveIntegerField(help_text="EVE type ID of the hull")
    hull_type_name = models.CharField(max_length=100, blank=True, default="")
    dna = models.TextField(
        blank=True, default="",
        help_text="EVE fitting DNA string (fitting -> export to clipboard). Optional.",
    )
    skill_tier = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Label for skill requirements, e.g. 'T2', 'T1', 'Newbie'",
    )
    description = models.TextField(blank=True, default="")
    active = models.BooleanField(default=True)

    # Rental pricing for contract/hangar_request rentals.
    # Admin sets the rate per billing period; members just pick duration.
    # Set any rate to 0 to disable that billing period for this fit.
    rent_rate_hourly = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"),
        help_text="ISK per hour for contract/hangar rentals. 0 = hourly not offered.")
    rent_rate_daily = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"),
        help_text="ISK per day for contract/hangar rentals. 0 = daily not offered.")
    rent_rate_weekly = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"),
        help_text="ISK per week for contract/hangar rentals. 0 = weekly not offered.")

    # Free-use hangar pricing. When a ship is taken from a free-use hangar,
    # the plugin auto-creates a rental and bills at this rate per period.
    # Set rate to 0 to disable free-use for this fit.
    free_use_rate = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"),
        help_text="ISK per billing period for free-use hangar rentals. Set 0 to disable free-use.")
    free_use_billing_period = models.CharField(
        max_length=10, choices=BillingPeriod.CHOICES,
        default=app_settings.SHIPFINANCE_DEFAULT_BILLING_PERIOD,
        help_text="Billing period for free-use rentals (hourly/daily/weekly).")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Doctrine Fit"
        verbose_name_plural = "Doctrine Fits"
        permissions = [
            ("access_shipfinance", "Can Access the Ship Finance App"),
            ("manage_shipfinance", "Can Manage Ship Finance (admin)"),
            ("use_rent", "Can Rent Ships"),
            ("use_finance", "Can Finance Ships"),
        ]

    def __str__(self):
        return self.name

    @property
    def available_stock_count(self):
        return self.stock.filter(state=ShipStockState.AVAILABLE).count()

    @property
    def rental_options(self):
        """Return list of (billing_period, rate) tuples with non-zero rates."""
        options = []
        if self.rent_rate_hourly > 0:
            options.append((BillingPeriod.HOURLY, self.rent_rate_hourly))
        if self.rent_rate_daily > 0:
            options.append((BillingPeriod.DAILY, self.rent_rate_daily))
        if self.rent_rate_weekly > 0:
            options.append((BillingPeriod.WEEKLY, self.rent_rate_weekly))
        return options


class FreeUseHangar(models.Model):
    """A corp hangar division designated as a self-service rental hangar.

    This is division-based, not location-based. Setting division 3 means
    Division 3 at ANY station is a rental hangar. Ships named with the
    prefix in that division at any station are rental ships.

    Members take ships from this hangar without filling out a form. The plugin
    auto-detects the ship leaving and bills the member for actual time used.

    Ships are identified by a **name prefix** set by the admin (e.g. '.BANR').
    Any ship in the designated division whose name starts with the prefix is
    a rental ship. Ships without the prefix are ignored (so random stuff in
    the hangar is safe).

    The admin names ships in-game with the prefix (e.g. '.BANR Gila 1').
    The plugin discovers ships by scanning the division for prefixed names,
    then tracks them by ESI item_id (stable for the life of the assembled ship).

    If require_return_to_origin is True, the rental only closes when the ship
    is returned to the SAME station it was rented from. Returning it to a
    different station keeps the rental open until the ship is back at the origin.

    Ships are matched to DoctrineFit by hull type_id for pricing.
    """

    hangar_division = models.PositiveSmallIntegerField(
        default=1, unique=True,
        help_text="Corp hangar division (1-7). This division at ANY station "
                  "is a rental hangar.")
    ship_name_prefix = models.CharField(
        max_length=20, default=".BANR",
        help_text="Ships in this division whose name starts with this prefix are "
                  "rental ships. Others are ignored. E.g. '.BANR' matches '.BANR Gila 1'.")
    require_return_to_origin = models.BooleanField(
        default=False,
        help_text="If True, the rental only closes when the ship is returned to "
                  "the SAME station it was rented from. Returning to a different "
                  "station keeps the rental open until the ship is back at the origin.")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["hangar_division"]
        verbose_name = "Rental Hangar"
        verbose_name_plural = "Rental Hangars"

    def __str__(self):
        return f"Division {self.hangar_division} ({self.ship_name_prefix})"


class ShipStock(models.Model):
    """A physical assembled ship the corp owns, registered for rent/finance.

    Tracked by ESI item_id (stable for the life of the assembled ship,
    regardless of what the ship is named). Ship names are NOT used for
    tracking — see README op-sec notes.
    """

    doctrine_fit = models.ForeignKey(
        DoctrineFit, on_delete=models.CASCADE, related_name="stock")
    item_id = models.BigIntegerField(unique=True, help_text="ESI asset item_id")
    item_name = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Current ship name (display only, not used for tracking)")
    location_id = models.BigIntegerField(help_text="Home station/structure location_id")
    location_name = models.CharField(max_length=200, blank=True, default="")
    hangar_division = models.PositiveSmallIntegerField(
        default=1, help_text="Corp hangar division (1-7) where the ship lives")
    state = models.CharField(
        max_length=20, choices=ShipStockState.CHOICES,
        default=ShipStockState.AVAILABLE)
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="registered_stock")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Ship Stock"
        verbose_name_plural = "Ship Stock"

    def __str__(self):
        return f"{self.doctrine_fit.name} (item {self.item_id})"


# ---------------------------------------------------------------------------
# Rental
# ---------------------------------------------------------------------------

class RentalAgreement(models.Model):
    """One rental: a member takes a ship for a period and pays a fee."""

    ship_stock = models.ForeignKey(
        ShipStock, on_delete=models.PROTECT, related_name="rentals")
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rentals")
    member_character = models.ForeignKey(
        EveCharacter, on_delete=models.SET_NULL, null=True,
        related_name="ship_rentals")

    delivery_mode = models.CharField(
        max_length=20, choices=DeliveryMode.CHOICES,
        default=DeliveryMode.CONTRACT)

    start_time = models.DateTimeField(default=timezone.now)
    # For contract/hangar_request: the agreed return deadline.
    # For free-use: null (no preset duration — billed for actual time used).
    due_date = models.DateTimeField(null=True, blank=True)
    return_detected_at = models.DateTimeField(null=True, blank=True)

    # For self-service rentals: where the ship was rented from.
    # Used when require_return_to_origin is True on the hangar.
    origin_location_id = models.BigIntegerField(null=True, blank=True)
    origin_location_name = models.CharField(max_length=200, blank=True, default="")

    billing_period = models.CharField(
        max_length=10, choices=BillingPeriod.CHOICES,
        default=app_settings.SHIPFINANCE_DEFAULT_BILLING_PERIOD)
    rate = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"),
        help_text="ISK per billing period")

    # Link to the invoices plugin Invoice for the rental fee.
    invoice = models.OneToOneField(
        "invoices.Invoice", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="rental_agreement")

    status = models.CharField(
        max_length=20, choices=RentalStatus.CHOICES,
        default=RentalStatus.ACTIVE)

    terms_acknowledged = models.BooleanField(default=False)
    terms_text = models.TextField(blank=True, default="")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RentalAgreementManager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Rental Agreement"
        verbose_name_plural = "Rental Agreements"

    def __str__(self):
        return f"Rental {self.id}: {self.ship_stock.doctrine_fit.name} -> {self.member}"

    @property
    def is_overdue(self):
        return (self.status == RentalStatus.ACTIVE
                and self.due_date is not None
                and timezone.now() > self.due_date)

    @property
    def duration_display(self):
        from datetime import timedelta
        if self.due_date is None:
            # Free-use: show actual duration so far (or until return)
            end = self.return_detected_at or timezone.now()
            delta = end - self.start_time
        else:
            delta = self.due_date - self.start_time
        days = delta.total_seconds() / 86400
        if days >= 1:
            return f"{days:.1f} days"
        hours = delta.total_seconds() / 3600
        return f"{hours:.1f} hours"


# ---------------------------------------------------------------------------
# Finance
# ---------------------------------------------------------------------------

class FinanceOffer(models.Model):
    """An admin-defined finance product for a doctrine fit.

    Members accept an offer to finance a ship; the plugin computes the
    installment schedule and creates invoices via the invoices plugin.
    """

    doctrine_fit = models.ForeignKey(
        DoctrineFit, on_delete=models.CASCADE, related_name="finance_offers")
    name = models.CharField(max_length=100, help_text="e.g. 'Gila Finance - 3 month'")
    principal = models.DecimalField(
        max_digits=20, decimal_places=2, help_text="Ship price in ISK")
    term_months = models.PositiveIntegerField(
        default=3, help_text="Number of monthly installments")
    interest_type = models.CharField(
        max_length=10, choices=InterestType.CHOICES,
        default=app_settings.SHIPFINANCE_DEFAULT_INTEREST_TYPE)
    interest_rate = models.DecimalField(
        max_digits=6, decimal_places=2,
        default=app_settings.SHIPFINANCE_DEFAULT_INTEREST_RATE,
        help_text="Flat % add-on, or APR % (annual)")

    insurance_enabled = models.BooleanField(default=True)
    insurance_premium_rate = models.DecimalField(
        max_digits=6, decimal_places=2,
        default=app_settings.SHIPFINANCE_DEFAULT_INSURANCE_PREMIUM_RATE,
        help_text="Insurance premium as % of principal")
    insurance_coverage = models.CharField(
        max_length=20, choices=InsuranceCoverage.CHOICES,
        default=app_settings.SHIPFINANCE_DEFAULT_INSURANCE_COVERAGE)
    insurance_flat_amount = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"),
        help_text="Used only when coverage = FLAT_AMOUNT")

    terms_text = models.TextField(
        blank=True, default="",
        help_text="Fine print shown to members before they accept.")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Finance Offer"
        verbose_name_plural = "Finance Offers"

    def __str__(self):
        return f"{self.name} ({self.doctrine_fit.name})"


class FinanceAgreement(models.Model):
    """One finance: a member pays installments for a ship or GeorgeForge deposit.

    For normal ship finance: finance_offer and ship_stock are set, the member
    gets the ship up front and pays monthly installments.

    For GeorgeForge installment plans: finance_offer and ship_stock are null,
    georgeforge_order_id is set. The member splits the full GeorgeForge order
    cost (including any deposit) into monthly installments. The GF order only
    advances to DEPOSIT_RECIEVED (ready to build) when this finance is fully
    paid off. The member gets the ship from GeorgeForge when it's delivered.
    """

    finance_offer = models.ForeignKey(
        FinanceOffer, on_delete=models.PROTECT, related_name="agreements",
        null=True, blank=True)
    ship_stock = models.ForeignKey(
        ShipStock, on_delete=models.PROTECT, related_name="finances",
        null=True, blank=True)
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="finances")
    member_character = models.ForeignKey(
        EveCharacter, on_delete=models.SET_NULL, null=True,
        related_name="ship_finances")

    # GeorgeForge installment plan: the GF order being financed (null for normal ship finance)
    georgeforge_order_id = models.BigIntegerField(
        null=True, blank=True,
        help_text="GeorgeForge order ID if this is an installment plan for a GF order.")
    georgeforge_item_name = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Item name from the GeorgeForge order (for display).")

    # Snapshot of offer terms at acceptance (so later offer edits don't
    # change active agreements).
    principal = models.DecimalField(max_digits=20, decimal_places=2)
    term_months = models.PositiveIntegerField()
    interest_type = models.CharField(max_length=10, choices=InterestType.CHOICES)
    interest_rate = models.DecimalField(max_digits=6, decimal_places=2)
    total_amount = models.DecimalField(max_digits=20, decimal_places=2)
    monthly_payment = models.DecimalField(max_digits=20, decimal_places=2)

    insurance_purchased = models.BooleanField(default=False)
    insurance_premium = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"))
    insurance_coverage = models.CharField(
        max_length=20, choices=InsuranceCoverage.CHOICES,
        default=InsuranceCoverage.REMAINING_BALANCE)
    insurance_flat_amount = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0"))
    insurance_invoice = models.OneToOneField(
        "invoices.Invoice", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="finance_insurance_agreement")

    status = models.CharField(
        max_length=25, choices=FinanceStatus.CHOICES,
        default=FinanceStatus.ACTIVE)
    paid_off_date = models.DateTimeField(null=True, blank=True)

    terms_acknowledged = models.BooleanField(default=False)
    terms_text = models.TextField(blank=True, default="")
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = FinanceAgreementManager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Finance Agreement"
        verbose_name_plural = "Finance Agreements"

    def __str__(self):
        if self.georgeforge_order_id:
            return f"Finance {self.id}: GF Order #{self.georgeforge_order_id} -> {self.member}"
        if self.ship_stock:
            return f"Finance {self.id}: {self.ship_stock.doctrine_fit.name} -> {self.member}"
        return f"Finance {self.id} -> {self.member}"

    @property
    def is_georgeforge(self):
        return self.georgeforge_order_id is not None

    @property
    def item_display_name(self):
        """Human-readable name for what's being financed."""
        if self.is_georgeforge:
            return self.georgeforge_item_name or f"GF Order #{self.georgeforge_order_id}"
        if self.ship_stock:
            return self.ship_stock.doctrine_fit.name
        return "Unknown"

    @property
    def installments_paid(self):
        return self.installments.filter(invoice__paid=True).count()

    @property
    def installments_total(self):
        return self.term_months

    @property
    def is_paid_off(self):
        return self.installments_total > 0 and self.installments_paid >= self.installments_total

    @property
    def remaining_balance(self):
        """Remaining principal+interest still owed (sum of unpaid installments)."""
        from django.db.models import Sum
        unpaid = self.installments.filter(invoice__paid=False).aggregate(
            total=Sum("amount"))["total"]
        return unpaid or Decimal("0")

    @property
    def insurance_coverage_amount(self):
        """How much the insurance would pay out if the ship were destroyed now."""
        if not self.insurance_purchased:
            return Decimal("0")
        if self.insurance_coverage == InsuranceCoverage.REMAINING_BALANCE:
            return self.remaining_balance
        elif self.insurance_coverage == InsuranceCoverage.PRINCIPAL:
            return self.principal
        elif self.insurance_coverage == InsuranceCoverage.FLAT_AMOUNT:
            return self.insurance_flat_amount
        return Decimal("0")


class FinanceInstallment(models.Model):
    """One monthly installment of a finance agreement, backed by an Invoice."""

    finance_agreement = models.ForeignKey(
        FinanceAgreement, on_delete=models.CASCADE, related_name="installments")
    invoice = models.OneToOneField(
        "invoices.Invoice", on_delete=models.PROTECT,
        related_name="finance_installment")
    installment_number = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    due_date = models.DateTimeField()
    is_final = models.BooleanField(default=False)

    class Meta:
        ordering = ["installment_number"]
        unique_together = [("finance_agreement", "installment_number")]
        verbose_name = "Finance Installment"
        verbose_name_plural = "Finance Installments"

    def __str__(self):
        return f"Installment {self.installment_number}/{self.finance_agreement.term_months} - {self.amount}"

    @property
    def is_paid(self):
        return self.invoice.paid if self.invoice_id else False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog(models.Model):
    """Append-only log of state changes for dispute resolution."""

    rental_agreement = models.ForeignKey(
        RentalAgreement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_entries")
    finance_agreement = models.ForeignKey(
        FinanceAgreement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_entries")
    ship_stock = models.ForeignKey(
        ShipStock, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_entries")
    action = models.CharField(max_length=50)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="shipfinance_audit_actions")
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit Log Entry"
        verbose_name_plural = "Audit Log"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} - {self.action}"


