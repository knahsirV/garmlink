"""Async wrapper around the synchronous garminconnect library."""

from __future__ import annotations

import asyncio
import random
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from .cache import ACTIVITY_TTL, HEALTH_TTL, STATIC_TTL, TTLCache
from .logs import logger, record_cache, safe_error

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
        # Serialises the first login. A cold start bursts several tool calls at
        # once; without this each would start its own Garmin login.
        self._auth_lock = asyncio.Lock()
        self._authenticated_at: float | None = None
        self._last_auth_error: str | None = None

    def authenticate(self) -> None:
        """Blocking Garmin login. Driven by _ensure_authenticated(), not called
        directly from async code — it performs network I/O.

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

    async def _ensure_authenticated(self) -> None:
        """Authenticate on first use, exactly once across concurrent callers."""
        if self._api is not None:
            return
        async with self._auth_lock:
            if self._api is not None:
                return  # another task authenticated while we waited
            loop = asyncio.get_running_loop()
            started = time.perf_counter()
            try:
                await loop.run_in_executor(self._executor, self.authenticate)
            except Exception as exc:
                self._authenticated_at = None
                self._last_auth_error = safe_error(exc)
                logger.error("garmin.login", extra={"fields": {
                    "outcome": "error",
                    "dur_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error": self._last_auth_error,
                }})
                raise
            self._authenticated_at = time.time()
            self._last_auth_error = None
            logger.info("garmin.login", extra={"fields": {
                "outcome": "ok",
                "dur_ms": round((time.perf_counter() - started) * 1000, 1),
            }})

    def auth_status(self) -> dict:
        """Garmin session state, for the /readyz endpoint."""
        if self._last_auth_error is not None:
            state = "error"
        elif self._api is not None:
            state = "authenticated"
        else:
            state = "never"
        return {
            "garmin": state,
            "authenticated_at": self._authenticated_at,
            "last_error": self._last_auth_error,
        }

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

        # Cache lookup happens before authentication, so a cached read still
        # succeeds while Garmin is unreachable.
        if cache_key is not None:
            sentinel = object()
            hit = self._cache.get(cache_key, default=sentinel)
            # Counted per tool call rather than logged per lookup: a range tool
            # makes one of these per day and would otherwise flood the stream.
            record_cache(hit=hit is not sentinel)
            if hit is not sentinel:
                return hit

        await self._ensure_authenticated()

        loop = asyncio.get_running_loop()
        reauthed = False
        attempt = 0

        while True:
            if attempt > 0:
                delay = _BASE_DELAY * (2 ** (attempt - 1))
                jitter = delay * _JITTER * (2 * random.random() - 1)
                await asyncio.sleep(delay + jitter)

            # Re-resolved each pass: a re-auth replaces self._api, so a method
            # bound to the old object would call into a dead session.
            method_fn = getattr(self._api, method_name, None)
            if method_fn is None or not callable(method_fn):
                raise ValueError(f"Unknown Garmin API method: {method_name!r}")

            try:
                result = await loop.run_in_executor(
                    self._executor, partial(method_fn, *args, **kwargs)
                )
            except GarminConnectAuthenticationError:
                # The session died mid-flight. Re-authenticate once and retry;
                # a second failure propagates rather than looping.
                if reauthed:
                    logger.error("garmin.reauth", extra={"fields": {
                        "method": method_name, "outcome": "gave_up",
                    }})
                    raise
                reauthed = True
                self._api = None
                logger.warning("garmin.reauth", extra={"fields": {
                    "method": method_name, "outcome": "retrying",
                }})
                await self._ensure_authenticated()
                continue
            except GarminConnectTooManyRequestsError:
                if attempt >= _MAX_RETRIES:
                    logger.error("garmin.retry", extra={"fields": {
                        "method": method_name, "attempt": attempt,
                        "outcome": "exhausted",
                    }})
                    raise
                attempt += 1
                # Without this the next loop sleeps for seconds in total
                # silence, which reads exactly like a hang in production.
                logger.warning("garmin.retry", extra={"fields": {
                    "method": method_name, "attempt": attempt,
                    "outcome": "rate_limited",
                }})
                continue

            if cache_key is not None:
                self._cache.set(cache_key, result, ttl)
            return result
