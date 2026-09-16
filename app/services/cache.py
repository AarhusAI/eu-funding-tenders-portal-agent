"""Shared cache with a pluggable backend.

Ported from the sibling ``AarhusAI/search-agent`` (``src/search_agent/cache.py``)
so both agents cache the same way — same protocol, same backend names, same
fail-open rule, same versioned keys. Divergences from it are deliberate and
noted where they occur: no ``bypass()`` contextvar (``/search`` has no
``no_cache`` flag) and a "memory" rather than "redis" default (this service
ships without a Redis).

One thing is cached here: the trimmed topic corpus (``app/services/corpus.py``).
``InMemoryBackend`` keeps it in the process heap — fine for one worker, but it
fragments per process, which is why ``RedisBackend`` exists.

All operations fail open: on any backend error the caller sees a miss and
carries on with the underlying work. A dead or slow cache must never break a
search — the same stance ``agent.handle`` takes on LLM failures.
"""

import contextlib
import hashlib
import json
import logging
import time
from typing import Any, Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.config import settings

log = logging.getLogger(__name__)

# Namespace version. Bump when a cached value's shape changes instead of
# flushing the backend. Keys look like `corpus:v1:<sha256>`.
#
# This is load-bearing here: the cached value is the output of ``corpus.trim``,
# so adding or renaming a trimmed field while old rows are still live would
# otherwise hand a new pod a dict missing the keys it expects.
_NAMESPACE_VERSION = "v1"


def make_key(namespace: str, *parts: Any) -> str:
    """Build a versioned, hashed cache key for ``namespace`` from ``parts``."""
    joined = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"{namespace}:{_NAMESPACE_VERSION}:{digest}"


class CacheBackend(Protocol):
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, ttl: int) -> None: ...
    async def close(self) -> None: ...


class DisabledBackend:
    """No-op backend — every get is a miss, every set is a drop."""

    async def get_json(self, key: str) -> Any | None:
        return None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        return None

    async def close(self) -> None:
        return None


class InMemoryBackend:
    """Process-local cache. Single-worker deployments, dev and tests.

    Returns the *same* object on every hit rather than a copy, so a caller that
    mutated a cached value would corrupt the cache. Safe today because
    ``profile.apply`` builds new dicts (``{**topic, ...}``) and never mutates
    its input — worth re-checking if that ever changes.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}

    async def get_json(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        # monotonic, not wall clock: a clock change must not make a fresh entry
        # look stale (and tests can control staleness without patching time).
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl)

    async def close(self) -> None:
        self._store.clear()


class RedisBackend:
    """Redis-backed cache with tight timeouts and fail-open semantics."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisBackend":
        # Sub-second timeouts: a slow Redis must not add latency on top of a
        # search that already pays a multi-second Portal fetch on a miss.
        client = aioredis.from_url(
            url,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
            decode_responses=False,
        )
        return cls(client)

    async def get_json(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(key)
        except (RedisError, TimeoutError, OSError) as exc:
            log.warning("cache get failed for %s: %s", key, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("cache value for %s was not valid JSON; treating as miss", key)
            return None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            log.warning("cache set failed to serialize value for %s: %s", key, exc)
            return
        try:
            await self._client.setex(key, ttl, payload)
        except (RedisError, TimeoutError, OSError) as exc:
            log.warning("cache set failed for %s: %s", key, exc)

    async def close(self) -> None:
        # Shutdown is not worth failing over: if the connection is already gone,
        # there is nothing left to release. (The reference agent writes this as
        # try/except/pass; this repo's ruff selects SIM, which wants suppress.)
        with contextlib.suppress(RedisError, OSError):
            await self._client.aclose()


_backend: CacheBackend | None = None


def init_cache() -> None:
    """Initialise the shared backend from settings. Called in lifespan startup.

    Eager rather than lazy so the configured backend is visible in the boot log
    and a Redis URL typo shows up at startup rather than on the first search.
    """
    global _backend
    if settings.cache_backend == "redis":
        _backend = RedisBackend.from_url(settings.cache_redis_url)
        log.info("cache backend=redis url=%s", settings.cache_redis_url)
    elif settings.cache_backend == "memory":
        _backend = InMemoryBackend()
        log.info("cache backend=memory (per-process; use redis for multi-instance)")
    else:
        _backend = DisabledBackend()
        log.info("cache backend=disabled — every search re-fetches the corpus")


async def close_cache() -> None:
    """Close the shared backend. Called in lifespan shutdown."""
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None


def get_backend() -> CacheBackend:
    """Return the shared backend, or a disabled one if never initialised."""
    if _backend is None:
        return DisabledBackend()
    return _backend


def set_backend_for_testing(backend: CacheBackend | None) -> None:
    """Swap the module-level backend (tests only)."""
    global _backend
    _backend = backend


async def get_json(key: str) -> Any | None:
    return await get_backend().get_json(key)


async def set_json(key: str, value: Any, ttl: int) -> None:
    await get_backend().set_json(key, value, ttl)
