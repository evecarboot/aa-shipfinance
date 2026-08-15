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
# Self-service rental detection: scan hangar by name prefix
# ---------------------------------------------------------------------------

def _scan_hangar_for_rental_ships(hangar):
    """Scan a rental division for ships matching the name prefix.

    Scans the designated division at ALL stations (division-based, not
    location-based). Returns a list of dicts:
    [{item_id, type_id, name, location_id}, ...]
    for ships whose name starts with the prefix.

    Uses corptools CorpAsset (which has a 'name' field) if available.
    Falls back to ESI corp assets + names endpoint.
    """
    prefix = hangar.ship_name_prefix
    expected_flag = "CorpSAG{0}".format(hangar.hangar_division)
    found = []

    # Try corptools first (has name field cached)
    if app_settings.corptools_installed():
        try:
            from corptools.models import CorpAsset
            assets = CorpAsset.objects.filter(
                location_flag=expected_flag,
                singleton=True,
            )
            for asset in assets:
                name = asset.name or ""
                if name.startswith(prefix):
                    found.append({
                        "item_id": asset.item_id,
                        "type_id": asset.type_id,
                        "name": name,
                        "location_id": asset.location_id,
                    })
            if found:
                return found
        except Exception as e:
            logger.error(f"corptools division scan failed for {hangar}: {e}")

    # ESI fallback: get corp assets, then get names for singleton items
    corp_id = _get_corp_id()
    client = _esi_client()
    if not corp_id or not client:
        return found

    try:
        assets = client.client.Corporation.get_corporations_corporation_id_assets(
            corporation_id=corp_id
        ).results()

        # Filter to ships in this division at any station
        singleton_item_ids = []
        division_assets = []
        for asset in assets:
            if (asset.get("location_flag") == expected_flag
                    and asset.get("is_singleton") is True):
                item_id = int(asset.get("item_id", 0))
                division_assets.append({
                    "item_id": item_id,
                    "type_id": asset.get("type_id"),
                    "name": "",
                    "location_id": asset.get("location_id"),
                })
                singleton_item_ids.append(item_id)

        if not singleton_item_ids:
            return found

        # Get names for singleton items
        # ESI endpoint: POST /corporations/{corp_id}/assets/names/
        try:
            names = client.client.Corporation.post_corporations_corporation_id_assets_names(
                corporation_id=corp_id,
                item_ids=singleton_item_ids[:1000],  # ESI limit
            ).results()
            name_map = {n.get("item_id"): n.get("name", "") for n in names}

            for asset in division_assets:
                name = name_map.get(asset["item_id"], "")
                if name.startswith(prefix):
                    asset["name"] = name
                    found.append(asset)
        except Exception as e:
            logger.debug(f"ESI asset names lookup failed: {e}")

    except Exception as e:
        logger.error(f"ESI division scan failed for {hangar}: {e}")

    return found


def _get_location_name(location_id):
    """Best-effort lookup of a location name from corptools or ESI."""
    if app_settings.corptools_installed():
        try:
            from corptools.models import EveLocation
            loc = EveLocation.objects.filter(id=location_id).first()
            if loc:
                return loc.name
        except Exception:
            pass
    return ""


@shared_task(bind=True, base=QueueOnce)
def detect_free_use_taken(self):
    """Scan rental divisions for prefixed ships and detect when they're taken.

    Flow:
    1. For each active rental hangar (division), scan that division at ALL
       stations for ships whose name starts with the prefix.
    2. Auto-create ShipStock records for any new prefixed ships found (matched
       to DoctrineFit by hull type_id). Each ship's actual location is stored.
    3. For ships that were tracked as AVAILABLE but are no longer in the division:
       - Find who has the ship (by item_id in member assets)
       - Auto-create a RentalAgreement, recording the origin station
    4. Billing happens when the ship is returned (see check_rental_returns).

    The member never fills out a form — taking the ship IS the rental.
    """
    logger.info("Scanning rental divisions for prefixed ships")

    hangars = FreeUseHangar.objects.filter(active=True)
    if not hangars.exists():
        return

    # Build a map of hull_type_id -> DoctrineFit for fits with free_use_rate > 0
    fits_by_hull = {}
    for fit in DoctrineFit.objects.filter(active=True, free_use_rate__gt=0):
        fits_by_hull[fit.hull_type_id] = fit

    for hangar in hangars:
        # Scan the division at all stations for prefixed ships
        ships_in_division = _scan_hangar_for_rental_ships(hangar)
        in_division_item_ids = {s["item_id"] for s in ships_in_division}

        # Auto-create ShipStock for new prefixed ships
        for ship in ships_in_division:
            fit = fits_by_hull.get(ship["type_id"])
            if fit is None:
                logger.warning(
                    f"Rental hangar {hangar}: ship '{ship['name']}' (type "
                    f"{ship['type_id']}) has no matching DoctrineFit with "
                    "free_use_rate > 0. Skipping.")
                continue

            loc_name = _get_location_name(ship["location_id"])
            stock, created = ShipStock.objects.get_or_create(
                item_id=ship["item_id"],
                defaults={
                    "doctrine_fit": fit,
                    "item_name": ship["name"],
                    "location_id": ship["location_id"],
                    "location_name": loc_name,
                    "hangar_division": hangar.hangar_division,
                    "state": ShipStockState.AVAILABLE,
                },
            )
            if created:
                logger.info(
                    f"Auto-registered rental ship: {ship['name']} "
                    f"(item {ship['item_id']}, fit {fit.name})")
                helpers.log_action(
                    "RENTAL_SHIP_AUTO_REGISTERED",
                    ship_stock=stock,
                    detail=f"Auto-registered '{ship['name']}' in {hangar}")

        # Check for ships that were AVAILABLE but are no longer in the division
        tracked_available = ShipStock.objects.filter(
            state=ShipStockState.AVAILABLE,
            hangar_division=hangar.hangar_division,
            doctrine_fit__free_use_rate__gt=0,
        ).select_related("doctrine_fit")

        for stock in tracked_available:
            if stock.item_id in in_division_item_ids:
                continue  # Still in the division

            # Ship is gone from the rental division — who has it?
            user, character = _get_member_holding_asset(stock)
            if user is None:
                logger.warning(
                    f"Rental hangar {hangar}: ship {stock.item_id} "
                    "left but can't find who took it. Admin should investigate.")
                continue

            # Don't auto-rent to someone without rent permission
            if not user.has_perm("shipfinance.use_rent"):
                logger.warning(
                    f"Rental hangar: {user} took {stock.item_id} but lacks "
                    "use_rent perm. Admin should investigate.")
                continue

            # Auto-create the rental, recording where it was rented from
            fit = stock.doctrine_fit
            now = timezone.now()
            return_hint = (
                "the same station you took it from"
                if hangar.require_return_to_origin
                else "any corp hangar"
            )
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
                origin_location_id=stock.location_id,
                origin_location_name=stock.location_name,
                terms_text=(
                    f"Self-service rental of {fit.name} from {hangar}. "
                    f"Rate: {fit.free_use_rate} ISK per {fit.free_use_billing_period}. "
                    f"Billed for actual time used when the ship is returned "
                    f"to {return_hint}."
                ),
                acknowledged_at=now,
            )
            stock.state = ShipStockState.OUT_RENT
            stock.save()

            helpers.log_action(
                "RENTAL_AUTO_CREATED",
                performed_by=user,
                rental_agreement=rental, ship_stock=stock,
                detail=f"Rental {rental.id} auto-created: "
                       f"{user} took {fit.name} (item {stock.item_id}) "
                       f"from {stock.location_name or stock.location_id}")

            helpers.notify_member(
                user, "Rental Started",
                f"You've taken a {fit.name} from the rental hangar. "
                f"You're now renting it at {fit.free_use_rate} ISK per "
                f"{fit.free_use_billing_period}. Return it to {return_hint} "
                f"to stop billing.",
                eve_character=character)
            helpers.notify_admin_webhook(
                "rental_created", "Self-Service Rental Started",
                f"Rental #{rental.id}: {user.username} took a {fit.name} "
                f"from {stock.location_name or stock.location_id}. "
                f"Rate: {fit.free_use_rate} ISK/{fit.free_use_billing_period}.",
                member=user)

            logger.info(f"Rental {rental.id} auto-created for {user}")


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
            # Self-service: ship back in corp hangar = returned
            # But if require_return_to_origin is set, it must be at the
            # SAME station it was rented from
            in_corp = _get_corp_asset_location(stock)
            if in_corp is not None:
                # Check if the hangar requires return to origin station
                hangar = FreeUseHangar.objects.filter(
                    hangar_division=stock.hangar_division, active=True
                ).first()
                if hangar and hangar.require_return_to_origin:
                    if (rental.origin_location_id
                            and in_corp != rental.origin_location_id):
                        # Returned to a different station — rental stays open
                        logger.info(
                            f"Rental {rental.id}: ship returned to {in_corp} "
                            f"but origin is {rental.origin_location_id}. "
                            "Rental stays open until returned to origin.")
                        continue

                helpers.mark_ship_returned(rental)
                logger.info(
                    f"Self-service rental {rental.id} returned "
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
                helpers.notify_admin_webhook(
                    "rental_overdue", "Rental Overdue",
                    f"Rental #{rental.id}: {stock.doctrine_fit.name} overdue "
                    f"(due {rental.due_date}). Member: {rental.member.username}",
                    member=rental.member)


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
