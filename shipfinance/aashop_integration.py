"""Optional aa-shop integration — installment plans for shop orders.

aa-shop is a public asset shop plugin. Members list items from their assets,
buyers place orders, and the shop owner manually contracts items in-game.

aa-shop has NO payment gate — delivery is manual. So installment plans work
on a manual-gate basis:

1. Buyer places an order on an aa-shop storefront (status = PENDING)
2. If the buyer is an Auth member, they come to this plugin and finance the
   order total via monthly installments
3. This plugin notifies the shop owner (via the admin webhook): "Order #ABC
   is on an installment plan — do not contract until paid off"
4. The shop owner holds the order manually (does not accept/contract)
5. When all installments are paid off, this plugin notifies: "Order #ABC
   paid off — ready to accept and contract"
6. The shop owner accepts the order and contracts as normal

Only Auth members can use installment plans (public buyers who aren't Auth
members can't be linked to a User account for financing).

This module is imported lazily and guarded by app_settings.aashop_installed()
so it never breaks if aa-shop is absent.
"""
import logging

from . import app_settings

logger = logging.getLogger(__name__)


def is_available():
    """True if aa-shop is installed and the integration can be used."""
    return app_settings.aashop_installed()


def get_member_orders(user):
    """Get a member's aa-shop orders that can be financed via installments.

    Matches aa-shop Buyer records to the Auth user by character ownership.
    Only returns PENDING orders (not yet accepted/contracted by the shop owner).

    Returns a list of dicts:
    [{id, reference, shop_name, estimated_total, status, item_summary}, ...]
    """
    if not is_available():
        return []
    try:
        from shop.models import Order, Buyer
        from allianceauth.authentication.models import CharacterOwnership
        from .models import FinanceAgreement

        # Find the user's characters
        char_ids = list(
            CharacterOwnership.objects.filter(user=user)
            .values_list("character_id", flat=True)
        )
        if not char_ids:
            return []

        # Find aa-shop Buyer records matching this user's characters
        buyers = Buyer.objects.filter(character_id__in=char_ids)
        if not buyers.exists():
            return []

        # Get pending orders for these buyers
        orders = Order.objects.filter(
            buyer__in=buyers,
            status=Order.PENDING,
        ).select_related("shop", "buyer").prefetch_related("lines")

        # Exclude orders that already have a finance agreement
        # Use aashop_order_id__isnull=False (BigIntegerField) not the CharField,
        # since CharField with default="" never stores NULL.
        financed_refs = set(
            FinanceAgreement.objects.filter(
                aashop_order_id__isnull=False
            ).exclude(aashop_order_reference="").values_list(
                "aashop_order_reference", flat=True)
        )

        result = []
        for order in orders:
            if order.reference in financed_refs:
                continue
            # Build a short item summary
            lines = list(order.lines.all())
            if lines:
                if len(lines) == 1:
                    summary = f"{lines[0].quantity}x {lines[0].eve_type.name}"
                else:
                    summary = f"{len(lines)} items ({lines[0].eve_type.name}...)"
            else:
                summary = "No items"

            result.append({
                "id": order.id,
                "reference": order.reference,
                "shop_name": order.shop.name if order.shop else "Unknown",
                "estimated_total": order.estimated_total,
                "status": order.status,
                "item_summary": summary,
            })
        return result
    except Exception as e:
        logger.error(f"Failed to get aa-shop orders for {user}: {e}", exc_info=True)
        return []


def get_order(order_id):
    """Get an aa-shop Order by ID. Returns the Order object or None."""
    if not is_available():
        return None
    try:
        from shop.models import Order
        return Order.objects.select_related("shop", "buyer").prefetch_related("lines").get(pk=order_id)
    except Exception as e:
        logger.error(f"Failed to get aa-shop order {order_id}: {e}")
        return None


def get_order_owner_user(order):
    """Try to find the Auth User who owns the shop that this order is for.

    Returns the shop's created_by User, or None.
    This is used to notify the shop owner about installment plan status.
    """
    try:
        return order.shop.created_by if order.shop else None
    except Exception:
        return None


def get_buyer_user(order):
    """Try to match an aa-shop order's Buyer to an Auth User via character ownership.

    Returns the User or None (public buyers won't have an Auth account).
    """
    try:
        from allianceauth.authentication.models import CharacterOwnership
        co = CharacterOwnership.objects.filter(
            character_id=order.buyer.character_id
        ).select_related("user").first()
        return co.user if co else None
    except Exception:
        return None
