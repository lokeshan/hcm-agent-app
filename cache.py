"""Tiny thread-safe TTL cache with stats — speeds up repeated Oracle HCM lookups.

Lives in-process (the MCP tools run in the app process via in-memory transport),
so cached entries persist across chat messages. Configurable via /admin.
"""
from __future__ import annotations
import threading, time

_store: dict = {}
_lock = threading.Lock()
_hits = 0
_misses = 0
MAX_ENTRIES = 5000   # bound memory: evict expired then oldest beyond this


def get(key: str):
    global _hits, _misses
    with _lock:
        item = _store.get(key)
        if item and item[1] > time.time():
            _hits += 1
            return item[0]
        if item:
            del _store[key]
        _misses += 1
        return None


def set(key: str, value, ttl: int) -> None:
    with _lock:
        _store[key] = (value, time.time() + max(1, int(ttl)))
        if len(_store) > MAX_ENTRIES:
            now = time.time()
            for k in [k for k, (_, exp) in _store.items() if exp <= now]:
                _store.pop(k, None)
            while len(_store) > MAX_ENTRIES:          # dict preserves insertion order → oldest first
                _store.pop(next(iter(_store)), None)


def clear(prefix: str | None = None) -> int:
    """Clear all entries, or only those whose key starts with `prefix`
    (used for per-source cache clearing, e.g. prefix='hcm:<connector_id>:')."""
    with _lock:
        if not prefix:
            n = len(_store)
            _store.clear()
            return n
        keys = [k for k in _store if k.startswith(prefix)]
        for k in keys:
            del _store[k]
        return len(keys)


def reset_stats() -> None:
    global _hits, _misses
    with _lock:
        _hits = 0
        _misses = 0


def stats() -> dict:
    with _lock:
        total = _hits + _misses
        return {
            "entries": len(_store),
            "hits": _hits,
            "misses": _misses,
            "hit_rate": round(_hits / total, 3) if total else 0.0,
        }
