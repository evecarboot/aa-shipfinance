"""Auth hooks: menu item, URL registration, secure group filter."""
from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import app_settings, urls


class ShipFinanceMenuItem(MenuItemHook):
    def __init__(self):
        MenuItemHook.__init__(
            self,
            _(app_settings.SHIPFINANCE_APP_NAME),
            "fas fa-rocket fa-fw",
            "shipfinance:index",
            navactive=["shipfinance:"],
        )

    def render(self, request):
        if request.user.has_perm("shipfinance.access_shipfinance"):
            return MenuItemHook.render(self, request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    return ShipFinanceMenuItem()


@hooks.register("url_hook")
def register_url():
    return UrlHook(urls, "shipfinance", r"^shipfinance/")


@hooks.register("secure_group_filters")
def filters():
    from .models import NoOverdueShipFinanceFilter
    return [NoOverdueShipFinanceFilter]
