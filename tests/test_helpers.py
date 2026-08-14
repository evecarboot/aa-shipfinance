"""Tests for shipfinance helpers (interest, insurance, schedule)."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from shipfinance import helpers
from shipfinance.models import (
    BillingPeriod,
    DeliveryMode,
    DoctrineFit,
    FinanceAgreement,
    FinanceOffer,
    InsuranceCoverage,
    InterestType,
    RentalAgreement,
    RentalStatus,
    ShipStock,
    ShipStockState,
)


class InterestCalcTest(TestCase):
    def test_flat_interest(self):
        total, monthly = helpers.compute_finance_schedule(
            principal=Decimal("1000"), term_months=10,
            interest_type=InterestType.FLAT, interest_rate=Decimal("10"))
        # total = 1000 * 1.10 = 1100, monthly = 110
        self.assertEqual(total, Decimal("1100.00"))
        self.assertEqual(monthly, Decimal("110.00"))

    def test_flat_interest_zero_rate(self):
        total, monthly = helpers.compute_finance_schedule(
            principal=Decimal("1000"), term_months=4,
            interest_type=InterestType.FLAT, interest_rate=Decimal("0"))
        self.assertEqual(total, Decimal("1000.00"))
        self.assertEqual(monthly, Decimal("250.00"))

    def test_apr_interest(self):
        total, monthly = helpers.compute_finance_schedule(
            principal=Decimal("1000"), term_months=12,
            interest_type=InterestType.APR, interest_rate=Decimal("12"))
        # APR 12% -> monthly rate 1%, amortizing payment ~88.85
        # total = 88.85 * 12 = 1066.20
        self.assertAlmostEqual(float(monthly), 88.85, places=1)
        self.assertAlmostEqual(float(total), 1066.20, places=1)

    def test_apr_zero_rate(self):
        total, monthly = helpers.compute_finance_schedule(
            principal=Decimal("1000"), term_months=4,
            interest_type=InterestType.APR, interest_rate=Decimal("0"))
        # No interest -> equal payments
        self.assertEqual(total, Decimal("1000.00"))
        self.assertEqual(monthly, Decimal("250.00"))


class RentalFeeTest(TestCase):
    def test_daily_billing_2_days(self):
        fee = helpers.compute_rental_fee(
            rate=Decimal("10000000"),
            billing_period=BillingPeriod.DAILY,
            duration_timedelta=timedelta(days=2))
        self.assertEqual(fee, Decimal("20000000.00"))

    def test_daily_billing_rounded_up(self):
        fee = helpers.compute_rental_fee(
            rate=Decimal("10000000"),
            billing_period=BillingPeriod.DAILY,
            duration_timedelta=timedelta(hours=30))
        # 30h = 1.25 days -> rounded up to 2 days -> 20M
        self.assertEqual(fee, Decimal("20000000.00"))

    def test_hourly_billing_3_hours(self):
        fee = helpers.compute_rental_fee(
            rate=Decimal("1000000"),
            billing_period=BillingPeriod.HOURLY,
            duration_timedelta=timedelta(hours=3))
        self.assertEqual(fee, Decimal("3000000.00"))

    def test_weekly_billing_2_weeks(self):
        fee = helpers.compute_rental_fee(
            rate=Decimal("50000000"),
            billing_period=BillingPeriod.WEEKLY,
            duration_timedelta=timedelta(days=14))
        self.assertEqual(fee, Decimal("100000000.00"))


class InsuranceTest(TestCase):
    def test_premium_calculation(self):
        premium = helpers.compute_insurance_premium(
            principal=Decimal("500000000"), premium_rate=Decimal("5"))
        self.assertEqual(premium, Decimal("25000000.00"))

    def test_premium_zero_rate(self):
        premium = helpers.compute_insurance_premium(
            principal=Decimal("500000000"), premium_rate=Decimal("0"))
        self.assertEqual(premium, Decimal("0.00"))


class InvoiceRefTest(TestCase):
    def test_ref_format(self):
        ref = helpers.generate_invoice_ref()
        self.assertTrue(ref.startswith("SF-"))
        # Should be opaque (8 hex chars after prefix)
        self.assertEqual(len(ref), 11)  # "SF-" + 8 chars

    def test_ref_uniqueness(self):
        refs = {helpers.generate_invoice_ref() for _ in range(100)}
        # Extremely unlikely to collide in 100 draws of 8 hex chars
        self.assertGreater(len(refs), 90)


class StateTransitionTest(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create(username="testuser")

        self.fit = DoctrineFit.objects.create(
            name="Test Gila", hull_type_id=11184, hull_type_name="Gila",
            skill_tier="T2")
        self.stock = ShipStock.objects.create(
            doctrine_fit=self.fit, item_id=1234567890,
            location_id=60000001, location_name="Test Station",
            hangar_division=1, state=ShipStockState.AVAILABLE)

    def test_mark_ship_returned(self):
        from django.utils import timezone
        rental = RentalAgreement.objects.create(
            ship_stock=self.stock, member=self.user,
            delivery_mode=DeliveryMode.CONTRACT,
            start_time=timezone.now(),
            due_date=timezone.now() + timedelta(hours=24),
            billing_period=BillingPeriod.DAILY,
            rate=Decimal("10000000"),
            status=RentalStatus.ACTIVE)
        self.stock.state = ShipStockState.OUT_RENT
        self.stock.save()

        helpers.mark_ship_returned(rental)

        self.stock.refresh_from_db()
        rental.refresh_from_db()
        self.assertEqual(self.stock.state, ShipStockState.AVAILABLE)
        self.assertEqual(rental.status, RentalStatus.RETURNED)
        self.assertIsNotNone(rental.return_detected_at)

    def test_mark_ship_destroyed_rental(self):
        from django.utils import timezone
        rental = RentalAgreement.objects.create(
            ship_stock=self.stock, member=self.user,
            delivery_mode=DeliveryMode.CONTRACT,
            start_time=timezone.now(),
            due_date=timezone.now() + timedelta(hours=24),
            billing_period=BillingPeriod.DAILY,
            rate=Decimal("10000000"),
            status=RentalStatus.ACTIVE)
        self.stock.state = ShipStockState.OUT_RENT
        self.stock.save()

        helpers.mark_ship_destroyed(rental, is_finance=False,
                                    killmail_url="https://zkillboard.com/kill/1/")

        self.stock.refresh_from_db()
        rental.refresh_from_db()
        self.assertEqual(self.stock.state, ShipStockState.DESTROYED)
        self.assertEqual(rental.status, RentalStatus.DESTROYED)


class FinancePayoffTest(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create(username="financeuser")
        self.fit = DoctrineFit.objects.create(
            name="Test Gila", hull_type_id=11184, hull_type_name="Gila")
        self.stock = ShipStock.objects.create(
            doctrine_fit=self.fit, item_id=9999999999,
            location_id=60000001, location_name="Test Station",
            hangar_division=1, state=ShipStockState.OUT_FINANCE)
        self.offer = FinanceOffer.objects.create(
            doctrine_fit=self.fit, name="Test Finance",
            principal=Decimal("300000000"), term_months=3,
            interest_type=InterestType.FLAT, interest_rate=Decimal("10"),
            insurance_enabled=False)
        total, monthly = helpers.compute_finance_schedule(
            self.offer.principal, self.offer.term_months,
            self.offer.interest_type, self.offer.interest_rate)
        self.fa = FinanceAgreement.objects.create(
            finance_offer=self.offer, ship_stock=self.stock,
            member=self.user, principal=self.offer.principal,
            term_months=self.offer.term_months,
            interest_type=self.offer.interest_type,
            interest_rate=self.offer.interest_rate,
            total_amount=total, monthly_payment=monthly,
            status="active")

    def test_paid_off_marks_stock_sold(self):
        helpers.mark_finance_paid_off(self.fa)
        self.stock.refresh_from_db()
        self.fa.refresh_from_db()
        self.assertEqual(self.stock.state, ShipStockState.SOLD)
        self.assertEqual(self.fa.status, "paid_off")
        self.assertIsNotNone(self.fa.paid_off_date)


class AuditLogTest(TestCase):
    def test_log_action_creates_entry(self):
        from django.contrib.auth.models import User
        user = User.objects.create(username="auditor")
        helpers.log_action("TEST_ACTION", performed_by=user, detail="test detail")
        from shipfinance.models import AuditLog
        self.assertEqual(AuditLog.objects.filter(action="TEST_ACTION").count(), 1)
