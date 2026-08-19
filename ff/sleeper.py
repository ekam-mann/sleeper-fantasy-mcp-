"""Thin client for Sleeper's public (read-only, no-auth) API.

Two hosts are used:
  api.sleeper.app/v1/...        - the documented league/roster/draft API
  api.sleeper.app/projections/  - undocumented, but it is where projections
                                  and ADP live. No auth either.

Everything is cached on disk so a chatty conversation doesn't hammer the API.
Sleeper asks callers to stay under 1000 requests/minute.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

BASE = "https://api.sleeper.app/v1"
PROJ = "https://api.sleeper.app/projections/nfl"

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

# How long each kind of response stays fresh, in seconds.
TTL_PLAYERS = 24 * 3600  # the full player dump is ~5MB and changes slowly
TTL_PROJECTIONS = 6 * 3600
TTL_LEAGUE = 300  # rosters/transactions move during a draft or waiver run
TTL_STATE = 3600


def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return CACHE_DIR / f"{safe}.json"


def _get(url: str, cache_key: str, ttl: int) -> Any:
    path = _cache_path(cache_key)
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # corrupt cache entry, just refetch

    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def clear_cache() -> int:
    """Drop every cached response. Returns how many files were removed."""
    files = list(CACHE_DIR.glob("*.json"))
    for f in files:
        f.unlink()
    return len(files)


# --- documented endpoints -------------------------------------------------


def nfl_state() -> dict:
    return _get(f"{BASE}/state/nfl", "state_nfl", TTL_STATE)


def league(league_id: str) -> dict:
    return _get(f"{BASE}/league/{league_id}", f"league_{league_id}", TTL_LEAGUE)


def league_users(league_id: str) -> list[dict]:
    return _get(f"{BASE}/league/{league_id}/users", f"users_{league_id}", TTL_LEAGUE)


def league_rosters(league_id: str) -> list[dict]:
    return _get(f"{BASE}/league/{league_id}/rosters", f"rosters_{league_id}", TTL_LEAGUE)


def matchups(league_id: str, week: int) -> list[dict]:
    return _get(
        f"{BASE}/league/{league_id}/matchups/{week}",
        f"matchups_{league_id}_{week}",
        TTL_LEAGUE,
    )


def transactions(league_id: str, week: int) -> list[dict]:
    return _get(
        f"{BASE}/league/{league_id}/transactions/{week}",
        f"tx_{league_id}_{week}",
        TTL_LEAGUE,
    )


def draft(draft_id: str) -> dict:
    return _get(f"{BASE}/draft/{draft_id}", f"draft_{draft_id}", TTL_LEAGUE)


def draft_picks(draft_id: str) -> list[dict]:
    # Deliberately short TTL: during a live draft this is the hot path, and a
    # stale board is worse than a few extra requests. 10s against a 120s pick
    # timer means you never miss the pick immediately before yours.
    return _get(f"{BASE}/draft/{draft_id}/picks", f"picks_{draft_id}", 10)


def user(username_or_id: str) -> dict:
    return _get(f"{BASE}/user/{username_or_id}", f"user_{username_or_id}", TTL_STATE)


def user_leagues(user_id: str, season: str) -> list[dict]:
    return _get(
        f"{BASE}/user/{user_id}/leagues/nfl/{season}",
        f"userleagues_{user_id}_{season}",
        TTL_LEAGUE,
    )


def trending(kind: str = "add", hours: int = 24, limit: int = 25) -> list[dict]:
    return _get(
        f"{BASE}/players/nfl/trending/{kind}?lookback_hours={hours}&limit={limit}",
        f"trending_{kind}_{hours}_{limit}",
        3600,
    )


def all_players() -> dict[str, dict]:
    """The full NFL player dictionary, keyed by player_id (~5MB)."""
    return _get(f"{BASE}/players/nfl", "players_nfl", TTL_PLAYERS)


# --- projections (undocumented) -------------------------------------------

_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]


def season_projections(season: str) -> list[dict]:
    """Full-season projected stat lines + ADP for every relevant player."""
    qs = "&".join(f"position[]={p}" for p in _POSITIONS)
    return _get(
        f"{PROJ}/{season}?season_type=regular&{qs}&order_by=adp_ppr",
        f"proj_season_{season}",
        TTL_PROJECTIONS,
    )


def week_projections(season: str, week: int) -> list[dict]:
    qs = "&".join(f"position[]={p}" for p in _POSITIONS)
    return _get(
        f"{PROJ}/{season}/{week}?season_type=regular&{qs}&order_by=ppr",
        f"proj_week_{season}_{week}",
        TTL_PROJECTIONS,
    )
