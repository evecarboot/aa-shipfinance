"""sitecustomize.py — loaded by Python at startup, before pytest.

Patches django_redis to use fakeredis so AA's task_statistics module can
initialize without a real Redis server during tests.
"""
import django_redis
import fakeredis

_fake_redis = fakeredis.FakeStrictRedis()


def _fake_get_redis_connection(alias="default", write=None, show_version=False):
    return _fake_redis


django_redis.get_redis_connection = _fake_get_redis_connection
