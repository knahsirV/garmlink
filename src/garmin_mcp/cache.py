"""Pure-Python TTL cache for Garmin MCP server."""

from __future__ import annotations

import threading
import time
from typing import Any

HEALTH_TTL: float = 300
ACTIVITY_TTL: float = 60
STATIC_TTL: float = 3600

# Key type: (method_name, args_tuple, kwargs_frozenset)
CacheKey = tuple[str, tuple, frozenset]


class TTLCache:
    """Thread-safe, dict-backed TTL cache using monotonic time."""

    def __init__(self) -> None:
        self._store: dict[CacheKey, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> Any | None:
        """Return cached value if not expired, else None."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: CacheKey, value: Any, ttl: float) -> None:
        """Store value with TTL (seconds)."""
        expires_at = time.monotonic() + ttl
        with self._lock:
            self._store[key] = (value, expires_at)

    def contains(self, key: CacheKey) -> bool:
        """Return True if key exists in cache and has not expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            _, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._store[key]
                return False
            return True

    def invalidate(self, method_name: str) -> None:
        """Remove all entries whose key starts with the given method name."""
        with self._lock:
            keys_to_delete = [k for k in self._store if k[0] == method_name]
            for k in keys_to_delete:
                del self._store[k]

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._store.clear()
