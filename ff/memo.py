"""In-process memoisation for the expensive derived tables.

The season and weekly stat feeds are cached on disk, but *deriving* a table
from them is not free: each build re-parses megabytes of JSON and walks every
player. A single `player` lookup asks for six of these tables - usage,
strength of schedule, form, volatility, xFP and availability - so with no memo
one question costs six full rebuilds. Measured at 23.5s per call, and flat on
repeat, because nothing was being kept.

This is a *process-lifetime* cache, deliberately distinct from the disk cache
in `sleeper.py`:

  - the disk cache decides when to re-ask the API, and has a TTL
  - this decides whether to recompute from bytes we already hold, and does not

That means a long-running server would keep serving a table built from data
that has since been refreshed. `clear()` exists for exactly that, and
`refresh_data` calls it alongside the disk wipe.

Returned tables are shared, not copied. Callers read them (`.get(player_id)`
and pull fields); nothing mutates them, and copying every table would give
back the cost this exists to remove.
"""

from __future__ import annotations

import functools
import hashlib
import json
import threading
from collections.abc import Callable
from typing import Any

_STORE: dict[str, dict[Any, Any]] = {}
_LOCK = threading.Lock()


def _normalise(value: Any) -> Any:
    """Make an argument hashable without losing what distinguishes it.

    Scoring settings arrive as dicts, which are unhashable and also the single
    most important part of the key - two leagues with different scoring must
    never share a cached table.
    """
    if isinstance(value, dict):
        blob = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        return "dict:" + hashlib.sha1(blob).hexdigest()[:16]
    if isinstance(value, (list, tuple)):
        return tuple(_normalise(v) for v in value)
    if isinstance(value, set):
        return ("set",) + tuple(sorted(_normalise(v) for v in value))
    return value


def table(fn: Callable) -> Callable:
    """Memoise a derived-table builder on its arguments."""
    name = f"{fn.__module__}.{fn.__qualname__}"

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = (
            tuple(_normalise(a) for a in args),
            tuple(sorted((k, _normalise(v)) for k, v in kwargs.items())),
        )
        bucket = _STORE.setdefault(name, {})
        # Check outside the lock first: hits are the common case and should not
        # serialise. A concurrent miss may compute twice, which is wasteful but
        # correct - these builders are pure functions of their arguments.
        if key in bucket:
            return bucket[key]

        result = fn(*args, **kwargs)
        with _LOCK:
            bucket[key] = result
        return result

    wrapper.cache_clear = lambda: _STORE.pop(name, None)  # type: ignore[attr-defined]
    return wrapper


def clear() -> int:
    """Drop every memoised table. Returns how many were held."""
    with _LOCK:
        held = sum(len(b) for b in _STORE.values())
        _STORE.clear()
    return held


def stats() -> dict[str, int]:
    """How many variants of each table are currently held."""
    return {name: len(bucket) for name, bucket in sorted(_STORE.items())}
