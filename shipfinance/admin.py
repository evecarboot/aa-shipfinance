from django.contrib import admin

from .models import (
    AuditLog,
    DoctrineFit,
    FinanceAgreement,
    FinanceInstallment,
    FinanceOffer,
    NoOverdueShipFinanceFilter,
    RentalAgreement,
    ShipStock,
)


@admin.register(DoctrineFit)
class DoctrineFitAdmin(admin.ModelAdmin):
    list_display = ("name", "hull_type_name", "skill_tier", "active", "available_stock_count")
    list_filter = ("active", "skill_tier")
    search_fields = ("name", "hull_type_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ShipStock)
class ShipStockAdmin(admin.ModelAdmin):
    list_display = ("doctrine_fit", "item_id", "location_name", "hangar_division", "state")
    list_filter = ("state", "doctrine_fit")
    search_fields = ("item_id", "item_name", "location_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(FinanceOffer)
class FinanceOfferAdmin(admin.ModelAdmin):
    list_display = ("name", "doctrine_fit", "principal", "term_months",
                    "interest_type", "interest_rate", "insurance_enabled", "active")
    list_filter = ("active", "interest_type", "insurance_enabled")
    search_fields = ("name", "doctrine_fit__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(RentalAgreement)
class RentalAgreementAdmin(admin.ModelAdmin):
    list_display = ("id", "ship_stock", "member", "delivery_mode",
                    "start_time", "due_date", "status")
    list_filter = ("status", "delivery_mode")
    search_fields = ("member__username", "ship_stock__doctrine_fit__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(FinanceAgreement)
class FinanceAgreementAdmin(admin.ModelAdmin):
    list_display = ("id", "ship_stock", "member", "principal",
                    "monthly_payment", "insurance_purchased", "status")
    list_filter = ("status", "insurance_purchased")
    search_fields = ("member__username", "ship_stock__doctrine_fit__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(FinanceInstallment)
class FinanceInstallmentAdmin(admin.ModelAdmin):
    list_display = ("finance_agreement", "installment_number", "amount",
                    "due_date", "is_paid", "is_final")
    list_filter = ("is_final",)
    search_fields = ("finance_agreement__ship_stock__doctrine_fit__name",)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "performed_by", "ship_stock")
    list_filter = ("action",)
    search_fields = ("detail", "action")
    readonly_fields = ("created_at",)


admin.site.register(NoOverdueShipFinanceFilter)
