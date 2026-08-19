"""Bye weeks, sourced from ESPN's public scoreboard API.

Sleeper's player dump has no bye_week data (the field isn't present at all), so
we derive byes from ESPN, which exposes an explicit `teamsOnBye` list per week.
No auth required.

One request per week, cached for a week — the NFL schedule doesn't move.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

TTL = 7 * 24 * 3600

# ESPN uses WSH; Sleeper uses WAS. Everything else lines up.
TEAM_FIXUPS = {"WSH": "WAS"}


def bye_weeks(season: str) -> dict[str, int]:
    """Map of Sleeper team abbreviation -> bye week for the given season."""
    path = CACHE_DIR / f"byes_{season}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < TTL:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    byes: dict[str, int] = {}
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for week in range(1, 19):
            try:
                resp = client.get(
                    ESPN, params={"dates": season, "seasontype": 2, "week": week}
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue  # a single bad week shouldn't sink the whole map

            for team in (data.get("week") or {}).get("teamsOnBye", []):
                abbr = team.get("abbreviation")
                if abbr:
                    byes[TEAM_FIXUPS.get(abbr, abbr)] = week

    # Only cache a result that actually looks complete.
    if len(byes) >= 30:
        path.write_text(json.dumps(byes), encoding="utf-8")
    return byes


def bye_conflicts(players: list[dict]) -> dict[int, list[str]]:
    """Group players by bye week so stacked byes are obvious.

    Returns only weeks where you have more than one player out.
    """
    grouped: dict[int, list[str]] = {}
    for p in players:
        wk = p.get("bye")
        if wk:
            grouped.setdefault(wk, []).append(f"{p['name']} ({p['position']})")
    return {wk: names for wk, names in sorted(grouped.items()) if len(names) > 1}
