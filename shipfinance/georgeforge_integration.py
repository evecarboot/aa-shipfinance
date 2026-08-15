"""Optional GeorgeForge integration — installment plans for full ship price.

GeorgeForge is a card builder tool. Admins list ships (ForSale) with a price
and optional deposit. Members place orders and normally pay upfront.

This plugin adds a **payment installments** option for GeorgeForge orders.
Instead of paying the full price upfront, a member can split the total order
cost into monthly installments through this plugin. The GeorgeForge order
only advances to building once the installments are fully paid off here.

IMPORTANT — the deposit is the gate:
GeorgeForge's order flow is:
  PENDING → AWAITING_DEPOSIT → DEPOSIT_RECIEVED → BUILDING → AWAITING_FINAL_PAYMENT → DELIVERED

If a GF item has NO deposit, GF skips AWAITING_DEPOSIT and goes straight to
building — there's no way to hold the order back. So installment plans only
work when the GF item has a deposit > 0. The deposit acts as the gate:
we cancel the deposit invoice, hold the order in AWAITING_DEPOSIT, and only
advance to DEPOSIT_RECIEVED when the installment plan is fully paid off.

When the installment plan is paid off, we also set order.paid = order.totalcost
so GF knows the full amount has been paid — no final payment is needed at the
contract stage.

Flow:
1. Admin creates a GF item with a deposit (the gate)
2. Member places an order in GeorgeForge (status = AWAITING_DEPOSIT)
3. Member comes to this plugin and sees their GeorgeForge orders
4. Member clicks "Pay in Installments"
5. This plugin creates a FinanceAgreement for the full order cost, split into
   monthly installments
6. The original GF deposit invoice is cancelled (we handle all payment)
7. Member pays installments via Alliance Auth invoices
8. When all installments are paid → set order.paid = totalcost and advance
   to DEPOSIT_RECIEVED so GeorgeForge proceeds to building
9. GeorgeForge builds and delivers — no final payment needed (already paid)

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

    Only returns orders that:
    - Are in AWAITING_DEPOSIT status (the gate exists)
    - Have deposit > 0 (without a deposit, GF skips straight to building
      and there's no way to hold the order for installments)
    - Don't already have a finance agreement from this plugin
    """
    if not is_available():
        return []
    try:
        from georgeforge.models import Order
        from .models import FinanceAgreement

        # Only orders with a deposit — the deposit is the gate that lets us
        # hold the order until installments are paid off.
        orders = Order.objects.filter(
            user=user,
            status=Order.OrderStatus.AWAITING_DEPOSIT,
            deposit__gt=0,
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
    Sets order.paid = order.totalcost (so GF knows the full amount is paid
    and doesn't expect a final payment at contract stage), then sets the
    order status to DEPOSIT_RECIEVED so GeorgeForge proceeds to building.

    Returns True on success, False on failure.
    """
    if not is_available():
        logger.warning("GeorgeForge not installed; cannot mark order ready")
        return False
    try:
        from georgeforge.models import Order
        order = Order.objects.get(pk=order_id)
        # Mark the full amount as paid so GF doesn't expect a final payment
        order.paid = order.totalcost
        order.status = Order.OrderStatus.DEPOSIT_RECIEVED
        order.save()
        logger.info(
            f"GeorgeForge order {order_id} marked as DEPOSIT_RECIEVED "
            f"(installment plan paid off — ready to build, paid={order.paid})")
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
