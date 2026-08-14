"""Tests for shipfinance models."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from shipfinance.models import (
    DeliveryMode,
    DoctrineFit,
    FinanceAgreement,
    FinanceOffer,
    InterestType,
    RentalAgreement,
    ShipStock,
    ShipStockState,
)


class DoctrineFitTest(TestCase):
    def test_create_fit(self):
        fit = DoctrineFit.objects.create(
            name="Gila T2", hull_type_id=11184, hull_type_name="Gila",
            skill_tier="T2")
        self.assertEqual(str(fit), "Gila T2")
        self.assertTrue(fit.active)
        self.assertEqual(fit.available_stock_count, 0)

    def test_available_stock_count(self):
        fit = DoctrineFit.objects.create(name="Gila", hull_type_id=11184)
        ShipStock.objects.create(
            doctrine_fit=fit, item_id=1, location_id=1, state=ShipStockState.AVAILABLE)
        ShipStock.objects.create(
            doctrine_fit=fit, item_id=2, location_id=1, state=ShipStockState.OUT_RENT)
        self.assertEqual(fit.available_stock_count, 1)


class ShipStockTest(TestCase):
    def test_create_stock(self):
        fit = DoctrineFit.objects.create(name="Gila", hull_type_id=11184)
        stock = ShipStock.objects.create(
            doctrine_fit=fit, item_id=1234567890, location_id=60000001,
            hangar_division=1)
        self.assertEqual(stock.state, ShipStockState.AVAILABLE)
        self.assertIn("item 1234567890", str(stock))


class FinanceAgreementTest(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="fuser")
        self.fit = DoctrineFit.objects.create(name="Gila", hull_type_id=11184)
        self.stock = ShipStock.objects.create(
            doctrine_fit=self.fit, item_id=1, location_id=1)
        self.offer = FinanceOffer.objects.create(
            doctrine_fit=self.fit, name="Test",
            principal=Decimal("300000000"), term_months=3,
            interest_type=InterestType.FLAT, interest_rate=Decimal("10"),
            insurance_enabled=False)

    def test_remaining_balance_no_installments(self):
        fa = FinanceAgreement.objects.create(
            finance_offer=self.offer, ship_stock=self.stock, member=self.user,
            principal=Decimal("300000000"), term_months=3,
            interest_type=InterestType.FLAT, interest_rate=Decimal("10"),
            total_amount=Decimal("330000000"), monthly_payment=Decimal("110000000"))
        self.assertEqual(fa.remaining_balance, Decimal("0"))
        self.assertEqual(fa.installments_paid, 0)
        self.assertFalse(fa.is_paid_off)


class RentalAgreementTest(TestCase):
    def test_is_overdue(self):
        user = User.objects.create(username="ruser")
        fit = DoctrineFit.objects.create(name="Gila", hull_type_id=11184)
        stock = ShipStock.objects.create(
            doctrine_fit=fit, item_id=1, location_id=1)
        past = timezone.now() - timedelta(hours=1)
        rental = RentalAgreement.objects.create(
            ship_stock=stock, member=user, delivery_mode=DeliveryMode.CONTRACT,
            start_time=timezone.now() - timedelta(hours=25),
            due_date=past, status="active")
        self.assertTrue(rental.is_overdue)

    def test_not_overdue(self):
        user = User.objects.create(username="ruser2")
        fit = DoctrineFit.objects.create(name="Gila2", hull_type_id=11184)
        stock = ShipStock.objects.create(
            doctrine_fit=fit, item_id=2, location_id=1)
        future = timezone.now() + timedelta(hours=24)
        rental = RentalAgreement.objects.create(
            ship_stock=stock, member=user, delivery_mode=DeliveryMode.CONTRACT,
            start_time=timezone.now(), due_date=future, status="active")
        self.assertFalse(rental.is_overdue)
