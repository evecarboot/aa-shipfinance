"""Optional GeorgeForge integration hook.

Only called when allianceauth-georgeforge is installed. Used to push a
ship to GeorgeForge's build queue (e.g. when a finance/savings plan completes,
or to create a contract for ship delivery).

This module is imported lazily and guarded by app_settings.georgeforge_installed()
so it never breaks if GeorgeForge is absent.
"""
import logging

from . import app_settings

logger = logging.getLogger(__name__)


def is_available():
    """True if GeorgeForge is installed and the integration can be used."""
    return app_settings.georgeforge_installed()


def create_delivery_contract(ship_stock, member_character, note=""):
    """Attempt to create a delivery contract via GeorgeForge.

    Returns True if a contract was created/handed off to GF, False otherwise.
    Implementations should catch GF-specific exceptions and log them — this
    must never raise into the calling view.
    """
    if not is_available():
        logger.debug("GeorgeForge not installed; skipping delivery contract")
        return False
    try:
        # GeorgeForge's API surface varies by version. This is a stub that
        # admins/integrators can fill in based on their GF version.
        # The intent: hand the ship to GF so it creates an in-game contract
        # from the corp to the member character.
        # import georgeforge  # noqa
        # georgeforge.api.create_contract(
        #     item_id=ship_stock.item_id,
        #     to_character=member_character,
        #     note=note,
        # )
        logger.warning(
            "GeorgeForge integration is a stub. Implement create_delivery_contract "
            "for your GF version. See shipfinance/georgeforge_integration.py.")
        return False
    except Exception as e:
        logger.error(f"GeorgeForge delivery contract failed: {e}", exc_info=True)
        return False
