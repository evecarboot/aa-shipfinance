"""conftest.py — patch fakeredis in before Django loads for AA task statistics.

The patch must happen at import time (top-level) because pytest-django calls
django.setup() during pytest_load_initial_conftests, which is before
pytest_configure runs.
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

# Patch django_redis before anything imports allianceauth.utils.cache
import django_redis  # noqa: E402
import fakeredis  # noqa: E402

_fake_redis = fakeredis.FakeStrictRedis()


def _fake_get_redis_connection(alias="default", write=None, show_version=False):
    return _fake_redis


django_redis.get_redis_connection = _fake_get_redis_connection

# Also patch it in allianceauth.utils.cache which imports the symbol by name
import allianceauth.utils.cache as aa_cache  # noqa: E402
aa_cache.get_redis_client = lambda: _fake_redis
