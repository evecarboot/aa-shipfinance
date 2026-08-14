"""Optional GeorgeForge integration — installment plans for full ship price.

GeorgeForge is a card builder tool. Admins list ships (ForSale) with a price
and optional deposit. Members place orders and normally pay upfront.

This plugin adds a **payment installments** option for GeorgeForge orders.
Instead of paying the full price upfront, a member can split the total order
cost into monthly installments through this plugin. The GeorgeForge order
only advances to building once the installments are fully paid off here.

If the order has a deposit, it's just part of the total being financed —
the member pays everything off in installments, then the ship gets built.

Flow:
1. Member places an order in GeorgeForge (status = AWAITING_DEPOSIT)
2. Member comes to this plugin and sees their GeorgeForge orders
3. Member clicks "Pay in Installments"
4. This plugin creates a FinanceAgreement for the full order cost, split into
   monthly installments
5. Member pays installments via Alliance Auth invoices
6. When all installments are paid → mark the GeorgeForge order as DEPOSIT_RECIEVED
   so GeorgeForge proceeds to building
7. GeorgeForge continues its normal flow (build, delivery)

This module is imported lazily and guarded by app_settings.georgeforge_installed()
so it never breaks if GeorgeForge is absent.
"""
import logging
from decimal import Decimal

from . import app_settings

logger = logging.getLogger(__name__)


def is_available():
    """True if GeorgeForge is installed and the integration can be used."""
    return app_settings.georgeforge_installed()


def get_member_orders(user):
    """Get a member's GeorgeForge orders that can be financed via installments.

    Returns a list of dicts with order info suitable for display:
    [{id, type_name, quantity, totalcost, deposit, status, status_display}, ...]

    Only returns orders in AWAITING_DEPOSIT status that don't already have
    a finance agreement from this plugin.
    """
    if not is_available():
        return []
    try:
        from georgeforge.models import Order
        from .models import FinanceAgreement

        # Orders awaiting deposit payment (the start of the GF payment flow)
        orders = Order.objects.filter(
            user=user,
            status=Order.OrderStatus.AWAITING_DEPOSIT,
        ).select_related("eve_type")

        # Exclude orders that already have a finance agreement
        financed_order_ids = FinanceAgreement.objects.filter(
            georgeforge_order_id__isnull=False
        ).values_list("georgeforge_order_id", flat=True)

        result = []
        for order in orders:
            if order.id in financed_order_ids:
                continue
            result.append({
                "id": order.id,
                "type_name": order.eve_type.name if order.eve_type else "Unknown",
                "quantity": order.quantity,
                "totalcost": order.totalcost,
                "deposit": order.deposit * order.quantity,
                "status": order.status,
                "status_display": order.get_status_display(),
            })
        return result
    except Exception as e:
        logger.error(f"Failed to get GeorgeForge orders for {user}: {e}", exc_info=True)
        return []


def get_order(order_id):
    """Get a GeorgeForge order by ID. Returns the Order object or None."""
    if not is_available():
        return None
    try:
        from georgeforge.models import Order
        return Order.objects.select_related("eve_type", "user").get(pk=order_id)
    except Exception as e:
        logger.error(f"Failed to get GeorgeForge order {order_id}: {e}")
        return None


def mark_order_ready_to_build(order_id):
    """Mark a GeorgeForge order as ready to build (deposit received).

    Called when a FinanceAgreement for this order is fully paid off.
    Sets the order status to DEPOSIT_RECIEVED so GeorgeForge continues
    its normal build/delivery flow.

    Returns True on success, False on failure.
    """
    if not is_available():
        logger.warning("GeorgeForge not installed; cannot mark order ready")
        return False
    try:
        from georgeforge.models import Order
        order = Order.objects.get(pk=order_id)
        if order.status != Order.OrderStatus.DEPOSIT_RECIEVED:
            order.status = Order.OrderStatus.DEPOSIT_RECIEVED
            order.save()
            logger.info(
                f"GeorgeForge order {order_id} marked as DEPOSIT_RECIEVED "
                f"(installment plan paid off — ready to build)")
        return True
    except Exception as e:
        logger.error(
            f"Failed to mark GeorgeForge order {order_id} as ready to build: {e}",
            exc_info=True)
        return False


def cancel_georgeforge_invoice(order_id):
    """Cancel the GeorgeForge deposit invoice for an order.

    When we finance the full order via installments, the original GF deposit
    invoice should be cancelled since this plugin handles payment.
    """
    if not is_available():
        return False
    try:
        from georgeforge.models import Order
        Order.cancel_invoice(order_id)
        logger.info(f"Cancelled GeorgeForge deposit invoice for order {order_id}")
        return True
    except Exception as e:
        logger.debug(f"Could not cancel GF invoice for order {order_id}: {e}")
        return False
