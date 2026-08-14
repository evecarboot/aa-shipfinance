"""Model managers for shipfinance."""
import logging

from django.db import models

logger = logging.getLogger(__name__)


class RentalAgreementQuerySet(models.QuerySet):
    def visible_to(self, user):
        if user.is_superuser:
            return self
        if user.has_perm("shipfinance.manage_shipfinance"):
            return self
        return self.filter(member=user)


class FinanceAgreementQuerySet(models.QuerySet):
    def visible_to(self, user):
        if user.is_superuser:
            return self
        if user.has_perm("shipfinance.manage_shipfinance"):
            return self
        return self.filter(member=user)


class RentalAgreementManager(models.Manager):
    def get_queryset(self):
        return RentalAgreementQuerySet(self.model, using=self._db).select_related(
            "ship_stock__doctrine_fit", "member", "member_character")

    def visible_to(self, user):
        return self.get_queryset().visible_to(user)


class FinanceAgreementManager(models.Manager):
    def get_queryset(self):
        return FinanceAgreementQuerySet(self.model, using=self._db).select_related(
            "finance_offer__doctrine_fit", "ship_stock__doctrine_fit",
            "member", "member_character")

    def visible_to(self, user):
        return self.get_queryset().visible_to(user)
