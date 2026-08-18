"""Async wrapper around the synchronous garminconnect library."""

from __future__ import annotations

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
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
            tokenstore=self._tokenstore_path,
        )
        try:
            api.login(self._tokenstore_path)
        except (FileNotFoundError, GarminConnectAuthenticationError):
            api.login()
        self._api = api

    def close(self) -> None:
        """Shut down the executor and clear the cache."""
        self._executor.shutdown(wait=True)
        self._cache.clear()

    async def call(
        self, method_name: str, *args: Any, ttl: float = HEALTH_TTL, **kwargs: Any
    ) -> Any:
        """Call a garminconnect method with caching and rate-limit retry.

        1. Check cache — return hit immediately.
        2. Run the garminconnect method in ThreadPoolExecutor.
        3. On GarminConnectTooManyRequestsError: exponential backoff with jitter,
           retry up to 3 times (~1 s, ~2 s, ~4 s).
        4. Store result in cache with ttl.
        5. Return result.
        """
        try:
            cache_key = (method_name, args, frozenset(kwargs.items()))
        except TypeError:
            cache_key = None  # skip cache for unhashable kwargs

        if cache_key is not None and self._cache.contains(cache_key):
            return self._cache.get(cache_key)

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
                self._cache.set(cache_key, result, ttl)
                return result
            except GarminConnectTooManyRequestsError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    raise
            except Exception:
                raise

        # Should never reach here, but satisfy type checker.
        raise last_exc  # type: ignore[misc]
