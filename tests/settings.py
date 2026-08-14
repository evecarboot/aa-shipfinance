"""Test settings for aa-shipfinance, modeled on invoices' test settings."""
from allianceauth.project_template.project_name.settings.base import *  # noqa

CELERY_ALWAYS_EAGER = True
CELERY_TASK_ALWAYS_EAGER = True

SITE_URL = "https://example.com"
CSRF_TRUSTED_ORIGINS = [SITE_URL]

INSTALLED_APPS += [  # noqa
    "eve_sde",
    "corptools",
    "invoices",
    "shipfinance",
]

ROOT_URLCONF = "tests.urls"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

ESI_SSO_CLIENT_ID = "123"
ESI_SSO_CLIENT_SECRET = "123"
ESI_SSO_CALLBACK_URL = "https://example.com/sso/callback"
ESI_USER_CONTACT_EMAIL = "test@example.com"

PAYMENT_CORP = 987654321

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
