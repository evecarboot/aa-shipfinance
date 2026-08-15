"""App-level settings for shipfinance, read from Django settings with sane defaults."""
import re

from django.apps import apps
from django.conf import settings


def get_site_url():
    """Derive the auth site base URL from the ESI callback URL."""
    regex = r"^(.+)\/s.+"
    matches = re.finditer(regex, settings.ESI_SSO_CALLBACK_URL, re.MULTILINE)
    url = "http://"
    for m in matches:
        url = m.groups()[0]
    return url


# Display name of the app in the Auth sidebar / page titles.
SHIPFINANCE_APP_NAME = getattr(settings, "SHIPFINANCE_APP_NAME", "Ship Finance")

# Payment corp ID (falls back to invoices' setting if not set here).
SHIPFINANCE_PAYMENT_CORP = getattr(
    settings,
    "SHIPFINANCE_PAYMENT_CORP",
    getattr(settings, "PAYMENT_CORP", None),
)

# Prefix for invoice refs. Keep opaque for op-sec; members see this in wallet
# journal entries. Default "SF-" is short and non-descriptive.
SHIPFINANCE_INVOICE_REF_PREFIX = getattr(settings, "SHIPFINANCE_INVOICE_REF_PREFIX", "SF")

# Default interest type for new finance offers: "flat" or "apr".
# Must match InterestType constants (lowercase).
SHIPFINANCE_DEFAULT_INTEREST_TYPE = getattr(
    settings, "SHIPFINANCE_DEFAULT_INTEREST_TYPE", "flat"
)

# Default interest rate (percentage, e.g. 10 = 10%).
SHIPFINANCE_DEFAULT_INTEREST_RATE = getattr(
    settings, "SHIPFINANCE_DEFAULT_INTEREST_RATE", 10
)

# Default insurance premium rate (% of principal) when insurance is enabled.
SHIPFINANCE_DEFAULT_INSURANCE_PREMIUM_RATE = getattr(
    settings, "SHIPFINANCE_DEFAULT_INSURANCE_PREMIUM_RATE", 5
)

# Default insurance coverage mode: "remaining_balance", "principal", "flat_amount".
# Must match InsuranceCoverage constants (lowercase).
SHIPFINANCE_DEFAULT_INSURANCE_COVERAGE = getattr(
    settings, "SHIPFINANCE_DEFAULT_INSURANCE_COVERAGE", "remaining_balance"
)

# Refunds on default. False = no refunds (default, trust + no-refund policy).
SHIPFINANCE_REFUNDS_ALLOWED = getattr(settings, "SHIPFINANCE_REFUNDS_ALLOWED", False)

# Default billing period for free-use rentals: "hourly", "daily", "weekly".
# Must match BillingPeriod constants (lowercase).
SHIPFINANCE_DEFAULT_BILLING_PERIOD = getattr(
    settings, "SHIPFINANCE_DEFAULT_BILLING_PERIOD", "daily"
)

# Default rental rate per period (ISK). 0 means admin must set per-fit.
SHIPFINANCE_DEFAULT_RENTAL_RATE = getattr(
    settings, "SHIPFINANCE_DEFAULT_RENTAL_RATE", 0
)

# Poll cadence (minutes) for asset/return detection tasks.
SHIPFINANCE_ASSET_POLL_MINUTES = getattr(settings, "SHIPFINANCE_ASSET_POLL_MINUTES", 60)

# Enable zKillboard API fallback for destroyed-ship detection.
SHIPFINANCE_ZKILL_FALLBACK = getattr(settings, "SHIPFINANCE_ZKILL_FALLBACK", True)

# Send Auth notifications for asset events (take/return/due/destroyed).
SHIPFINANCE_SEND_AUTH_NOTIFICATIONS = getattr(
    settings, "SHIPFINANCE_SEND_AUTH_NOTIFICATIONS", True
)


def discord_bot_active():
    """True if aadiscordbot v3+ is installed and available for notifications."""
    if apps.is_installed("aadiscordbot"):
        try:
            import aadiscordbot as ab
            version = ab.__version__.split(".")
            if int(version[0]) >= 3:
                return True
        except Exception:
            return False
    return False


SHIPFINANCE_SEND_DISCORD_NOTIFICATIONS = getattr(
    settings, "SHIPFINANCE_SEND_DISCORD_NOTIFICATIONS", True
)

# Discord webhook URL for admin activity logging. When set, the plugin posts
# events (rentals, returns, finances, defaults, etc.) to this Discord channel
# so admins can monitor activity. Set to None/empty to disable.
# Example: SHIPFINANCE_ADMIN_DISCORD_WEBHOOK = "https://discord.com/api/webhooks/123/abc"
SHIPFINANCE_ADMIN_DISCORD_WEBHOOK = getattr(
    settings, "SHIPFINANCE_ADMIN_DISCORD_WEBHOOK", None
)


def invoices_installed():
    """True if the allianceauth-invoices app is installed (required dependency)."""
    return apps.is_installed("invoices")


def georgeforge_installed():
    """True if allianceauth-georgeforge is installed (optional installment plans)."""
    return apps.is_installed("georgeforge")


def corptools_installed():
    """True if corp-tools is installed (used for asset tracking)."""
    return apps.is_installed("corptools")
