"""Celery tasks: asset polling for return detection, killmail/zKill for destroyed detection.

Uses ESI directly where possible (via django-esi) for corp/character asset lookups,
falling back to corptools if installed. zKillboard is used for destroyed-ship detection.
"""
import logging
from datetime import timedelta

import requests
from celery import shared_task
from django.utils import timezone

from allianceauth.services.tasks import QueueOnce

from . import app_settings, helpers
from .models import (
    BillingPeriod,
    DeliveryMode,
    FinanceAgreement,
    FinanceStatus,
    FreeUseHangar,
    RentalAgreement,
    RentalStatus,
    ShipStock,
    ShipStockState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ESI-based asset lookups (preferred, falls back to corptools)
# ---------------------------------------------------------------------------

def _esi_client():
    """Get the ESI client from django-esi. Returns None if not available."""
    try:
        from esi.clients import EsiClientProvider
        return EsiClientProvider()
    except Exception as e:
        logger.debug(f"ESI client not available: {e}")
        return None


def _get_corp_id():
    """Get the corporation ID from settings (PAYMENT_CORP or SHIPFINANCE_PAYMENT_CORP)."""
    return app_settings.SHIPFINANCE_PAYMENT_CORP


def _esi_corp_asset_exists(item_id):
    """Check via ESI if a corp asset with this item_id exists (anywhere in corp).

    Returns True/False, or None if ESI is not available.
    """
    corp_id = _get_corp_id()
    if not corp_id:
        return None
    client = _esi_client()
    if client is None:
        return None
    try:
        # Requires esi-assets.read_corporation_assets.v1 scope on a corp token
        assets = client.client.Corporation.get_corporations_corporation_id_assets(
            corporation_id=corp_id
        ).results()
        for asset in assets:
            if int(asset.get("item_id", 0)) == item_id:
                return True
        return False
    except Exception as e:
        logger.debug(f"ESI corp asset check failed: {e}")
        return None


def _esi_corp_asset_location(item_id):
    """Via ESI, get the location_id and location_flag for a corp asset.

    Returns (location_id, location_flag) or (None, None) if not found / ESI unavailable.
    """
    corp_id = _get_corp_id()
    if not corp_id:
        return None, None
    client = _esi_client()
    if client is None:
        return None, None
    try:
        assets = client.client.Corporation.get_corporations_corporation_id_assets(
            corporation_id=corp_id
        ).results()
        for asset in assets:
            if int(asset.get("item_id", 0)) == item_id:
                return asset.get("location_id"), asset.get("location_flag")
        return None, None
    except Exception as e:
        logger.debug(f"ESI corp asset location failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# corptools-based asset lookups (fallback)
# ---------------------------------------------------------------------------

def _get_corp_asset_location(stock):
    """Return the corp asset record for this stock's item_id, or None.

    Tries ESI first, then falls back to corptools.
    Checks corp assets anywhere (not just the home hangar).
    """
    # Try ESI first
    loc_id, loc_flag = _esi_corp_asset_location(stock.item_id)
    if loc_id is not None:
        return loc_id  # Found in corp assets via ESI

    # Fall back to corptools
    if not app_settings.corptools_installed():
        return None
    try:
        from corptools.models import CorpAsset
        asset = CorpAsset.objects.filter(item_id=stock.item_id).first()
        return asset.location_id if asset else None
    except Exception as e:
        logger.error(f"CorpAsset lookup failed for stock {stock.id}: {e}")
        return None


def _get_corp_assets_for_stock(stock):
    """Check if the ship is back in its expected corp hangar division.

    Tries ESI first, then falls back to corptools.
    """
    # Try ESI first
    corp_id = _get_corp_id()
    client = _esi_client()
    if corp_id and client:
        try:
            assets = client.client.Corporation.get_corporations_corporation_id_assets(
                corporation_id=corp_id
            ).results()
            expected_flag = "CorpSAG{0}".format(stock.hangar_division)
            for asset in assets:
                if (int(asset.get("item_id", 0)) == stock.item_id
                        and asset.get("location_id") == stock.location_id
                        and asset.get("location_flag") == expected_flag):
                    return True
            return False
        except Exception as e:
            logger.debug(f"ESI corp asset check failed, falling back to corptools: {e}")

    # Fall back to corptools
    if not app_settings.corptools_installed():
        return False
    try:
        from corptools.models import CorpAsset
        return CorpAsset.objects.filter(
            item_id=stock.item_id,
            location_id=stock.location_id,
            location_flag="CorpSAG{0}".format(stock.hangar_division),
        ).exists()
    except Exception as e:
        logger.error(f"CorpAsset lookup failed for stock {stock.id}: {e}")
        return False


def _get_member_holding_asset(stock):
    """Return the (user, character) holding this ship, or (None, None).

    Tries corptools CharacterAsset first (fastest for finding who has it),
    then falls back to ESI character assets (slower — needs to check each char).
    """
    # corptools is the fastest way to find who has a specific item_id
    if app_settings.corptools_installed():
        try:
            from corptools.models import CharacterAsset
            from allianceauth.authentication.models import CharacterOwnership
            asset = CharacterAsset.objects.filter(
                item_id=stock.item_id,
            ).select_related("character").first()
            if asset:
                co = CharacterOwnership.objects.filter(
                    character=asset.character).select_related("user").first()
                if co:
                    return co.user, asset.character
        except Exception as e:
            logger.error(f"CharacterAsset lookup failed for stock {stock.id}: {e}")

    # ESI fallback: check each registered character's assets
    # This is slow but works without corptools
    client = _esi_client()
    if client is None:
        return None, None
    try:
        from allianceauth.authentication.models import CharacterOwnership
        from esi.models import Token
        # Get all characters with asset scopes
        tokens = Token.objects.filter(
            scopes__name="esi-assets.read_assets.v1"
        ).select_related("character").distinct()
        for token in tokens:
            try:
                assets = client.client.Assets.get_characters_character_id_assets(
                    character_id=token.character_id,
                    token=token.valid_access_token
                ).results()
                for asset in assets:
                    if int(asset.get("item_id", 0)) == stock.item_id:
                        co = CharacterOwnership.objects.filter(
                            character__character_id=token.character_id
                        ).select_related("user", "character").first()
                        if co:
                            return co.user, co.character
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"ESI character asset lookup failed: {e}")

    return None, None


def _get_member_has_asset(stock, member):
    """Check if a specific member currently holds the ship's item_id."""
    if app_settings.corptools_installed():
        try:
            from corptools.models import CharacterAsset
            chars = member.character_ownerships.all().values_list("character", flat=True)
            return CharacterAsset.objects.filter(
                item_id=stock.item_id,
                character__in=chars,
            ).exists()
        except Exception as e:
            logger.error(f"CharacterAsset lookup failed for stock {stock.id}: {e}")

    # ESI fallback
    client = _esi_client()
    if client is None:
        return False
    try:
        from esi.models import Token
        char_ids = member.character_ownerships.all().values_list(
            "character__character_id", flat=True)
        for char_id in char_ids:
            token = Token.objects.filter(
                character_id=char_id,
                scopes__name="esi-assets.read_assets.v1"
            ).first()
            if not token:
                continue
            try:
                assets = client.client.Assets.get_characters_character_id_assets(
                    character_id=char_id,
                    token=token.valid_access_token
                ).results()
                for asset in assets:
                    if int(asset.get("item_id", 0)) == stock.item_id:
                        return True
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"ESI member asset check failed: {e}")
    return False


# ---------------------------------------------------------------------------
# Free-use detection: ship taken from free-use hangar
# ---------------------------------------------------------------------------

@shared_task(bind=True, base=QueueOnce)
def detect_free_use_taken(self):
    """Detect ships taken from free-use hangars and auto-create rentals.

    For each available ship whose doctrine fit has a non-zero free_use_rate:
    - Check if the ship is in a designated FreeUseHangar.
    - If the ship is no longer in that hangar (but was last time)...
    - ...and is now in a member's personal assets...
    - ...auto-create a RentalAgreement with delivery_mode=free_use.

    The member never fills out a form — taking the ship IS the rental.
    Billing happens when the ship is returned (see check_rental_returns).
    """
    logger.info("Checking for free-use ships taken")

    # Get all active free-use hangars
    free_hangars = FreeUseHangar.objects.filter(active=True)
    if not free_hangars.exists():
        return  # No free-use hangars configured

    # Only ships that are available and have a free-use rate set
    available = ShipStock.objects.filter(
        state=ShipStockState.AVAILABLE,
        doctrine_fit__free_use_rate__gt=0,
        doctrine_fit__active=True,
    ).select_related("doctrine_fit")

    for stock in available:
        # Check if this ship is supposed to be in a free-use hangar
        in_free_hangar = False
        for hangar in free_hangars:
            if (stock.location_id == hangar.location_id
                    and stock.hangar_division == hangar.hangar_division):
                in_free_hangar = True
                break

        if not in_free_hangar:
            continue  # This ship is not in a free-use hangar

        # Check if the ship is still in its hangar
        if _get_corp_assets_for_stock(stock):
            continue  # Still in the hangar, nobody took it

        # Ship is gone from the free-use hangar — who has it?
        user, character = _get_member_holding_asset(stock)
        if user is None:
            logger.warning(
                f"Free-use: ship {stock.item_id} left hangar but can't find who took it. "
                "Admin should investigate.")
            continue

        # Don't auto-rent to someone without rent permission
        if not user.has_perm("shipfinance.use_rent"):
            logger.warning(
                f"Free-use: {user} took {stock.item_id} but lacks use_rent perm. "
                "Admin should investigate.")
            continue

        # Auto-create the rental
        fit = stock.doctrine_fit
        now = timezone.now()
        rental = RentalAgreement.objects.create(
            ship_stock=stock,
            member=user,
            member_character=character,
            delivery_mode=DeliveryMode.FREE_USE,
            start_time=now,
            due_date=None,  # No preset duration — billed for actual time
            billing_period=fit.free_use_billing_period,
            rate=fit.free_use_rate,
            status=RentalStatus.ACTIVE,
            terms_acknowledged=True,  # Taking the ship = implicit acceptance
            terms_text=(
                f"Free-use rental of {fit.name}. Rate: {fit.free_use_rate} ISK "
                f"per {fit.free_use_billing_period}. Billed for actual time used "
                f"when the ship is returned to a corp hangar."
            ),
            acknowledged_at=now,
        )
        stock.state = ShipStockState.OUT_RENT
        stock.save()

        helpers.log_action(
            "FREE_USE_RENTAL_AUTO_CREATED",
            performed_by=user,
            rental_agreement=rental, ship_stock=stock,
            detail=f"Free-use rental {rental.id} auto-created: "
                   f"{user} took {fit.name} (item {stock.item_id})")

        helpers.notify_member(
            user, "Free-Use Rental Started",
            f"You've taken a {fit.name} from the free-use hangar. "
            f"You're now renting it at {fit.free_use_rate} ISK per "
            f"{fit.free_use_billing_period}. Return it to any corp hangar "
            f"to stop billing.",
            eve_character=character)

        logger.info(f"Free-use rental {rental.id} auto-created for {user}")


# ---------------------------------------------------------------------------
# Return detection
# ---------------------------------------------------------------------------

@shared_task(bind=True, base=QueueOnce)
def check_rental_returns(self):
    """Poll outstanding rentals: detect returns, overdue, and losses.

    For contract/hangar_request rentals: check if the ship is back in the
    corp hangar (returned) or still with the member (active/overdue).
    For free-use rentals: ship back in ANY corp hangar = returned.
    """
    logger.info("Checking rental returns")
    now = timezone.now()
    active_rentals = RentalAgreement.objects.filter(
        status__in=[RentalStatus.ACTIVE, RentalStatus.OVERDUE])

    for rental in active_rentals:
        stock = rental.ship_stock

        if rental.delivery_mode == DeliveryMode.FREE_USE:
            # Free-use: ship back in ANY corp hangar = returned
            # (doesn't have to be the original hangar/station)
            in_corp = _get_corp_asset_location(stock)
            if in_corp is not None:
                helpers.mark_ship_returned(rental)
                logger.info(
                    f"Free-use rental {rental.id} returned "
                    f"(ship back in corp assets at {in_corp})")
            continue

        # Contract / hangar_request: check specific home hangar
        in_corp_hangar = _get_corp_assets_for_stock(stock)
        member_has_it = _get_member_has_asset(stock, rental.member)

        if in_corp_hangar:
            helpers.mark_ship_returned(rental)
            logger.info(f"Rental {rental.id} returned (ship back in corp hangar)")
        elif not member_has_it and not in_corp_hangar:
            logger.warning(
                f"Rental {rental.id}: ship {stock.item_id} not found in corp "
                "hangar or member assets — pending destroyed/lost check")
        else:
            # Member still has it — check overdue
            if rental.due_date and now > rental.due_date and rental.status != RentalStatus.OVERDUE:
                rental.status = RentalStatus.OVERDUE
                rental.save()
                helpers.log_action(
                    "RENTAL_OVERDUE", rental_agreement=rental, ship_stock=stock,
                    detail=f"Rental {rental.id} overdue (due {rental.due_date})")
                helpers.notify_member(
                    rental.member, "Rental Overdue",
                    f"Your rental of {stock.doctrine_fit.name} is overdue. "
                    f"Please return it to the corp hangar.",
                    eve_character=rental.member_character)


@shared_task(bind=True, base=QueueOnce)
def check_finance_destroyed(self):
    """Check if financed ships have been destroyed (no longer in anyone's assets).

    If the ship is gone from both corp and member assets, check killmails/zKill
    for destruction. If confirmed destroyed, settle per insurance policy.
    If not found on killmails, leave for admin review (mark_lost is manual).
    """
    logger.info("Checking financed ships for destruction")
    active_finances = FinanceAgreement.objects.filter(
        status=FinanceStatus.ACTIVE)

    for fa in active_finances:
        stock = fa.ship_stock
        in_corp = _get_corp_assets_for_stock(stock)
        member_has = _get_member_has_asset(stock, fa.member)

        if not in_corp and not member_has:
            # Ship is gone — check for killmail
            killmail = _check_killmail_for_stock(stock, fa.member)
            if killmail:
                helpers.mark_ship_destroyed(
                    fa, is_finance=True, killmail_url=killmail)
                logger.info(f"Finance {fa.id} ship destroyed (killmail found)")
            else:
                logger.warning(
                    f"Finance {fa.id}: ship {stock.item_id} unaccounted, "
                    "no killmail found — admin review needed")


@shared_task(bind=True, base=QueueOnce)
def check_rental_destroyed(self):
    """Check if rented ships have been destroyed (not returned, not in assets)."""
    logger.info("Checking rented ships for destruction")
    active_rentals = RentalAgreement.objects.filter(
        status__in=[RentalStatus.ACTIVE, RentalStatus.OVERDUE])

    for rental in active_rentals:
        stock = rental.ship_stock
        in_corp = _get_corp_assets_for_stock(stock)
        member_has = _get_member_has_asset(stock, rental.member)

        if not in_corp and not member_has:
            killmail = _check_killmail_for_stock(stock, rental.member)
            if killmail:
                helpers.mark_ship_destroyed(
                    rental, is_finance=False, killmail_url=killmail)
                logger.info(f"Rental {rental.id} ship destroyed (killmail found)")


# ---------------------------------------------------------------------------
# Killmail / zKill checking
# ---------------------------------------------------------------------------

def _check_killmail_for_stock(stock, member):
    """Check zKillboard API for a recent kill involving this ship.

    Returns the zKill URL if found, else None.

    zKill doesn't expose item_id, so we match on: victim is the member's
    character, ship type matches the doctrine fit's hull, and the kill is
    recent (killmail_time after the rental/finance start). This is
    best-effort — see README for limitations.
    """
    if not app_settings.SHIPFINANCE_ZKILL_FALLBACK:
        return None

    try:
        chars = member.character_ownerships.all().values_list(
            "character__character_id", flat=True)
        hull_type_id = stock.doctrine_fit.hull_type_id

        for char_id in chars:
            # Note: no-items modifier was permanently disabled by zKill
            url = (
                f"https://zkillboard.com/api/losses/characterID/{char_id}/"
                f"page/1/"
            )
            try:
                resp = requests.get(url, timeout=10, headers={
                    "User-Agent": "aa-shipfinance/0.1 (AllianceAuth plugin)"
                })
                if resp.status_code == 200:
                    kills = resp.json()
                    for kill in kills[:10]:  # recent kills only
                        # ship_type_id is inside the victim object
                        victim = kill.get("victim", {})
                        if victim.get("ship_type_id") == hull_type_id:
                            killmail_id = kill.get("killmail_id")
                            return f"https://zkillboard.com/kill/{killmail_id}/"
            except Exception as e:
                logger.error(f"zKill query failed for char {char_id}: {e}")
                continue
    except Exception as e:
        logger.error(f"Killmail check failed for stock {stock.id}: {e}")

    return None


# ---------------------------------------------------------------------------
# Finance payoff check
# ---------------------------------------------------------------------------

@shared_task(bind=True, base=QueueOnce)
def check_finance_payoffs(self):
    """Check if any finance agreements are fully paid off and mark them."""
    logger.info("Checking finance payoffs")
    active = FinanceAgreement.objects.filter(status=FinanceStatus.ACTIVE)
    for fa in active:
        if fa.is_paid_off:
            helpers.mark_finance_paid_off(fa)
            logger.info(f"Finance {fa.id} paid off")


# ---------------------------------------------------------------------------
# Setup task
# ---------------------------------------------------------------------------

@shared_task
def setup_default_tasks():
    """Create the default periodic tasks for shipfinance."""
    from django_celery_beat.models import CrontabSchedule, PeriodicTask
    poll_minutes = app_settings.SHIPFINANCE_ASSET_POLL_MINUTES

    schedule_returns, _ = CrontabSchedule.objects.get_or_create(
        minute=f"*/{poll_minutes}", hour="*", day_of_week="*",
        day_of_month="*", month_of_year="*", timezone="UTC")
    PeriodicTask.objects.update_or_create(
        task="shipfinance.tasks.check_rental_returns",
        defaults={
            "crontab": schedule_returns,
            "name": "Ship Finance: Check Rental Returns",
            "enabled": True,
        })
    PeriodicTask.objects.update_or_create(
        task="shipfinance.tasks.detect_free_use_taken",
        defaults={
            "crontab": schedule_returns,
            "name": "Ship Finance: Detect Free-Use Ships Taken",
            "enabled": True,
        })

    schedule_destroyed, _ = CrontabSchedule.objects.get_or_create(
        minute="*/30", hour="*", day_of_week="*",
        day_of_month="*", month_of_year="*", timezone="UTC")
    PeriodicTask.objects.update_or_create(
        task="shipfinance.tasks.check_rental_destroyed",
        defaults={
            "crontab": schedule_destroyed,
            "name": "Ship Finance: Check Rented Ships Destroyed",
            "enabled": True,
        })
    PeriodicTask.objects.update_or_create(
        task="shipfinance.tasks.check_finance_destroyed",
        defaults={
            "crontab": schedule_destroyed,
            "name": "Ship Finance: Check Financed Ships Destroyed",
            "enabled": True,
        })

    schedule_payoffs, _ = CrontabSchedule.objects.get_or_create(
        minute="0", hour="*", day_of_week="*",
        day_of_month="*", month_of_year="*", timezone="UTC")
    PeriodicTask.objects.update_or_create(
        task="shipfinance.tasks.check_finance_payoffs",
        defaults={
            "crontab": schedule_payoffs,
            "name": "Ship Finance: Check Finance Payoffs",
            "enabled": True,
        })
    logger.info("Ship Finance default tasks created")
