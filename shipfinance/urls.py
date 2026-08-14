from django.urls import path

from . import views

app_name = "shipfinance"

urlpatterns = [
    path("", views.index, name="index"),

    # Member views
    path("browse/", views.browse_ships, name="browse"),
    path("rent/<int:fit_id>/", views.rent_ship, name="rent_ship"),
    path("finance/<int:offer_id>/", views.finance_ship, name="finance_ship"),
    path("my-rentals/", views.my_rentals, name="my_rentals"),
    path("my-finances/", views.my_finances, name="my_finances"),

    # Admin: doctrine fits
    path("admin/", views.admin_dashboard, name="admin_dashboard"),
    path("admin/fits/", views.admin_fits, name="admin_fits"),
    path("admin/fits/new/", views.admin_fit_edit, name="admin_fit_new"),
    path("admin/fits/<int:fit_id>/", views.admin_fit_edit, name="admin_fit_edit"),
    path("admin/fits/<int:fit_id>/delete/", views.admin_fit_delete, name="admin_fit_delete"),

    # Admin: stock
    path("admin/stock/", views.admin_stock, name="admin_stock"),
    path("admin/stock/new/", views.admin_stock_edit, name="admin_stock_new"),
    path("admin/stock/<int:stock_id>/", views.admin_stock_edit, name="admin_stock_edit"),
    path("admin/stock/<int:stock_id>/delete/", views.admin_stock_delete, name="admin_stock_delete"),

    # Admin: free-use hangars
    path("admin/hangars/", views.admin_hangars, name="admin_hangars"),
    path("admin/hangars/new/", views.admin_hangar_edit, name="admin_hangar_new"),
    path("admin/hangars/<int:hangar_id>/", views.admin_hangar_edit, name="admin_hangar_edit"),
    path("admin/hangars/<int:hangar_id>/delete/", views.admin_hangar_delete, name="admin_hangar_delete"),

    # Admin: finance offers
    path("admin/offers/", views.admin_offers, name="admin_offers"),
    path("admin/offers/new/", views.admin_offer_edit, name="admin_offer_new"),
    path("admin/offers/<int:offer_id>/", views.admin_offer_edit, name="admin_offer_edit"),
    path("admin/offers/<int:offer_id>/delete/", views.admin_offer_delete, name="admin_offer_delete"),

    # Admin: agreements
    path("admin/rentals/", views.admin_rentals, name="admin_rentals"),
    path("admin/finances/", views.admin_finances, name="admin_finances"),
    path("admin/rentals/<int:rental_id>/returned/", views.admin_mark_returned, name="admin_mark_returned"),
    path("admin/rentals/<int:rental_id>/destroyed/", views.admin_mark_destroyed_rental, name="admin_mark_destroyed_rental"),
    path("admin/rentals/<int:rental_id>/lost/", views.admin_mark_lost_rental, name="admin_mark_lost_rental"),
    path("admin/finances/<int:finance_id>/paidoff/", views.admin_mark_paid_off, name="admin_mark_paid_off"),
    path("admin/finances/<int:finance_id>/defaulted/", views.admin_mark_defaulted, name="admin_mark_defaulted"),
    path("admin/finances/<int:finance_id>/destroyed/", views.admin_mark_destroyed_finance, name="admin_mark_destroyed_finance"),
    path("admin/finances/<int:finance_id>/lost/", views.admin_mark_lost_finance, name="admin_mark_lost_finance"),

    # Admin: audit log
    path("admin/audit/", views.admin_audit, name="admin_audit"),

    # Admin: setup tasks
    path("admin/setup-tasks/", views.admin_setup_tasks, name="admin_setup_tasks"),
]
