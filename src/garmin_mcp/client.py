"""Async wrapper around the synchronous garminconnect library."""

from __future__ import annotations

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectTooManyRequestsError,
)

from .cache import ACTIVITY_TTL, HEALTH_TTL, STATIC_TTL, TTLCache

__all__ = ["GarminClient"]

_MAX_RETRIES = 3
_BASE_DELAY = 1.0
_JITTER = 0.2


class GarminClient:
    """Async-friendly Garmin Connect client with caching and retry logic."""

    def __init__(self, email: str, password: str, tokenstore_path: str) -> None:
        self._email = email
        self._password = password
        self._tokenstore_path = tokenstore_path
        self._api: Garmin | None = None
        self._cache = TTLCache()
        self._executor = ThreadPoolExecutor(max_workers=4)

    def authenticate(self) -> None:
        """Synchronous authentication. Called once at server startup.

        Tries loading from tokenstore first; falls back to email+password login.
        """
        api = Garmin(
            email=self._email,
            password=self._password,
            is_cn=False,
            prompt_mfa=None,
            return_on_mfa=False,
        )
        api.login(self._tokenstore_path)
        self._api = api

    def invalidate(self, method_name: str) -> None:
        """Drop every cached entry for the given garminconnect method."""
        self._cache.invalidate(method_name)

    def close(self) -> None:
        """Shut down the executor and clear the cache."""
        self._executor.shutdown(wait=True)
        self._cache.clear()

    async def call(
        self,
        method_name: str,
        *args: Any,
        ttl: float = HEALTH_TTL,
        cache: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Call a garminconnect method with caching and rate-limit retry.

        1. Check cache — return hit immediately.
        2. Run the garminconnect method in ThreadPoolExecutor.
        3. On GarminConnectTooManyRequestsError: exponential backoff with jitter,
           retry up to 3 times (~1 s, ~2 s, ~4 s).
        4. Store result in cache with ttl.
        5. Return result.

        Args:
            cache: Set False for mutating calls (uploads, renames, scheduling).
                Writes must never be served from — or stored in — the read cache,
                otherwise a repeated write returns the first call's response
                without the write actually happening.
        """
        cache_key = None
        if cache:
            try:
                candidate = (method_name, args, frozenset(kwargs.items()))
                # Build AND hash here: constructing the tuple never hashes its
                # members, so an unhashable arg (e.g. a workout dict) would
                # otherwise only blow up later, inside the cache lookup.
                hash(candidate)
            except TypeError:
                cache_key = None  # unhashable argument — skip the cache
            else:
                cache_key = candidate

        if cache_key is not None:
            sentinel = object()
            hit = self._cache.get(cache_key, default=sentinel)
            if hit is not sentinel:
                return hit

        if self._api is None:
            raise RuntimeError("GarminClient.authenticate() must be called before call()")

        method_fn = getattr(self._api, method_name, None)
        if method_fn is None or not callable(method_fn):
            raise ValueError(f"Unknown Garmin API method: {method_name!r}")
        loop = asyncio.get_running_loop()

        last_exc: GarminConnectTooManyRequestsError | None = None
        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                delay = _BASE_DELAY * (2 ** (attempt - 1))
                jitter = delay * _JITTER * (2 * random.random() - 1)
                await asyncio.sleep(delay + jitter)
            try:
                result = await loop.run_in_executor(
                    self._executor, partial(method_fn, *args, **kwargs)
                )
                if cache_key is not None:
                    self._cache.set(cache_key, result, ttl)
                return result
            except GarminConnectTooManyRequestsError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    raise

        # Should never reach here, but satisfy type checker.
        raise last_exc  # type: ignore[misc]
