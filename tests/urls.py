"""Test URL conf for aa-shipfinance."""
from django.urls import include, path

urlpatterns = [
    path("shipfinance/", include("shipfinance.urls", namespace="shipfinance")),
    path("invoice/", include("invoices.urls", namespace="invoices")),
]
