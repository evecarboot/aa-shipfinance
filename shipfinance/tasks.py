"""Celery tasks: asset polling for return detection, killmail/zKill for destroyed detection."""
import logging

import requests
from celery import shared_task
from django.utils import timezone

from allianceauth.services.tasks import QueueOnce

from . import app_settings, helpers
from .models import (
    FinanceAgreement,
    FinanceStatus,
    RentalAgreement,
    RentalStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Asset-based return detection
# ---------------------------------------------------------------------------

def _get_corp_assets_for_stock(stock):
    """Check corp-tools CorpAsset for the stock's item_id in its home hangar.

    Returns True if the ship is back in the expected corp hangar division
    at the expected location.
    """
    if not app_settings.corptools_installed():
        logger.warning("corptools not installed; cannot do asset-based return detection")
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


def _get_member_has_asset(stock, member):
    """Check if the member currently holds the ship's item_id in their assets.

    Uses corp-tools CharacterAsset if available.
    """
    if not app_settings.corptools_installed():
        return False
    try:
        from corptools.models import CharacterAsset
        chars = member.character_ownerships.all().values_list("character", flat=True)
        return CharacterAsset.objects.filter(
            item_id=stock.item_id,
            character__in=chars,
        ).exists()
    except Exception as e:
        logger.error(f"CharacterAsset lookup failed for stock {stock.id}: {e}")
        return False


@shared_task(bind=True, base=QueueOnce)
def check_rental_returns(self):
    """Poll outstanding rentals: detect returns, overdue, and losses.

    For contract/hangar_request rentals: check if the ship is back in the
    corp hangar (returned) or still with the member (active/overdue).
    For free-use rentals: same logic — ship back = returned.
    """
    logger.info("Checking rental returns")
    now = timezone.now()
    active_rentals = RentalAgreement.objects.filter(
        status__in=[RentalStatus.ACTIVE, RentalStatus.OVERDUE])

    for rental in active_rentals:
        stock = rental.ship_stock
        in_corp_hangar = _get_corp_assets_for_stock(stock)
        member_has_it = _get_member_has_asset(stock, rental.member)

        if in_corp_hangar:
            # Ship is back home — mark returned
            helpers.mark_ship_returned(rental)
            logger.info(f"Rental {rental.id} returned (ship back in corp hangar)")
        elif not member_has_it and not in_corp_hangar:
            # Ship is nowhere we can see — either destroyed or lost
            # Don't auto-mark; let the destroyed-detection task or admin handle it
            logger.warning(
                f"Rental {rental.id}: ship {stock.item_id} not found in corp "
                "hangar or member assets — pending destroyed/lost check")
        else:
            # Member still has it
            if now > rental.due_date and rental.status != RentalStatus.OVERDUE:
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
    """Check zKillboard API for a recent kill involving this ship's item_id.

    Returns the zKill URL if found, else None.

    Note: zKill doesn't expose item_id directly, but killmails include the
    ship type and victim. We match on: victim is the member's character,
    ship type matches the doctrine fit's hull, and the kill is recent
    (within the rental/finance period). This is best-effort — see README
    for limitations.
    """
    if not app_settings.SHIPFINANCE_ZKILL_FALLBACK:
        return None

    try:
        chars = member.character_ownerships.all().values_list(
            "character__character_id", flat=True)
        hull_type_id = stock.doctrine_fit.hull_type_id

        for char_id in chars:
            url = (
                f"https://zkillboard.com/api/losses/characterID/{char_id}/"
                f"no-items/page/1/"
            )
            try:
                resp = requests.get(url, timeout=10, headers={
                    "User-Agent": "aa-shipfinance/0.1 (AllianceAuth plugin)"
                })
                if resp.status_code == 200:
                    kills = resp.json()
                    for kill in kills[:10]:  # recent kills only
                        if kill.get("ship_type_id") == hull_type_id:
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
