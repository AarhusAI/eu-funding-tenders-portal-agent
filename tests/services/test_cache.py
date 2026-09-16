"""The pluggable cache backend: keys, the three implementations, and fail-open.

Mirrors the coverage of the sibling AarhusAI/search-agent's tests/test_cache.py,
which this module was ported from. The fail-open cases are the ones that matter:
they are the difference between a Redis outage costing latency and costing the
whole search.
"""

from unittest.mock import AsyncMock

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services import cache

# --- key construction ---------------------------------------------------------


def test_a_key_carries_its_namespace_and_version():
    assert cache.make_key("corpus", "anything").startswith("corpus:v1:")


def test_the_same_inputs_always_produce_the_same_key():
    assert cache.make_key("corpus", "a", 1) == cache.make_key("corpus", "a", 1)


def test_different_inputs_produce_different_keys():
    assert cache.make_key("corpus", "a") != cache.make_key("corpus", "b")


def test_different_namespaces_do_not_collide():
    assert cache.make_key("corpus", "a") != cache.make_key("other", "a")


def test_bumping_the_namespace_version_invalidates_every_key(monkeypatch):
    """The documented way to invalidate after trim() changes shape."""
    before = cache.make_key("corpus", "a")
    monkeypatch.setattr(cache, "_NAMESPACE_VERSION", "v2")
    assert cache.make_key("corpus", "a") != before


# --- DisabledBackend ----------------------------------------------------------


async def test_the_disabled_backend_never_returns_a_hit():
    backend = cache.DisabledBackend()
    await backend.set_json("k", {"v": 1}, ttl=60)
    assert await backend.get_json("k") is None
    await backend.close()


# --- InMemoryBackend ----------------------------------------------------------


async def test_the_memory_backend_round_trips_a_value():
    backend = cache.InMemoryBackend()
    await backend.set_json("k", {"v": 1}, ttl=60)
    assert await backend.get_json("k") == {"v": 1}


async def test_the_memory_backend_misses_on_an_unknown_key():
    assert await cache.InMemoryBackend().get_json("missing") is None


async def test_an_expired_memory_entry_is_evicted_on_read():
    """Eviction is lazy — there is no reaper, the read does it."""
    backend = cache.InMemoryBackend()
    backend._store["k"] = ("stale", 0.0)  # already expired
    assert await backend.get_json("k") is None
    assert "k" not in backend._store


async def test_closing_the_memory_backend_drops_everything():
    backend = cache.InMemoryBackend()
    await backend.set_json("k", "v", ttl=60)
    await backend.close()
    assert await backend.get_json("k") is None


# --- RedisBackend -------------------------------------------------------------


@pytest.fixture
def redis_backend() -> cache.RedisBackend:
    return cache.RedisBackend(fakeredis.FakeAsyncRedis(decode_responses=False))


async def test_the_redis_backend_round_trips_a_value(redis_backend):
    await redis_backend.set_json("k", {"v": 1}, ttl=60)
    assert await redis_backend.get_json("k") == {"v": 1}


async def test_the_redis_backend_misses_on_an_unknown_key(redis_backend):
    assert await redis_backend.get_json("missing") is None


async def test_a_failing_redis_get_is_a_miss_not_an_error(redis_backend):
    redis_backend._client.get = AsyncMock(side_effect=RedisConnectionError("boom"))
    assert await redis_backend.get_json("k") is None


async def test_a_failing_redis_set_is_swallowed(redis_backend):
    redis_backend._client.setex = AsyncMock(side_effect=RedisConnectionError("boom"))
    await redis_backend.set_json("k", "v", ttl=60)  # must not raise


async def test_a_corrupt_cached_value_is_treated_as_a_miss(redis_backend):
    """Written by an older namespace version, or by something else entirely."""
    await redis_backend._client.set("k", b"{not json")
    assert await redis_backend.get_json("k") is None


async def test_a_value_that_cannot_be_serialised_is_dropped_not_raised(redis_backend):
    await redis_backend.set_json("k", {"fn": lambda: None}, ttl=60)
    assert await redis_backend.get_json("k") is None


async def test_closing_the_redis_backend_swallows_a_failing_client(redis_backend):
    redis_backend._client.aclose = AsyncMock(side_effect=RedisConnectionError("boom"))
    await redis_backend.close()  # must not raise


async def test_from_url_builds_a_client_with_sub_second_timeouts():
    """A slow Redis must not add latency to a search that already pays a fetch."""
    backend = cache.RedisBackend.from_url("redis://localhost:6379/0")
    kwargs = backend._client.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == 0.3
    assert kwargs["socket_timeout"] == 0.3
    await backend.close()


# --- backend selection and the module-level singleton -------------------------


async def test_init_cache_selects_the_backend_named_in_settings(monkeypatch):
    for name, expected in [
        ("memory", cache.InMemoryBackend),
        ("redis", cache.RedisBackend),
        ("disabled", cache.DisabledBackend),
    ]:
        monkeypatch.setattr(cache.settings, "cache_backend", name)
        cache.init_cache()
        assert isinstance(cache.get_backend(), expected)
        await cache.close_cache()


async def test_an_uninitialised_cache_is_disabled_rather_than_an_error():
    """Nothing should explode if a code path runs outside the app lifespan."""
    cache.set_backend_for_testing(None)
    assert isinstance(cache.get_backend(), cache.DisabledBackend)
    await cache.set_json("k", "v", ttl=60)
    assert await cache.get_json("k") is None


async def test_close_cache_is_safe_to_call_twice():
    cache.set_backend_for_testing(None)
    await cache.close_cache()
    await cache.close_cache()


async def test_the_module_level_helpers_use_the_installed_backend():
    cache.set_backend_for_testing(cache.InMemoryBackend())
    await cache.set_json("k", [1, 2], ttl=60)
    assert await cache.get_json("k") == [1, 2]
