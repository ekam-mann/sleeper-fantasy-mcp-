"""Availability history - how often a player is actually on the field.

Deliberately *not* called injury history. The underlying data is games played
per season, which conflates three different things:

  - genuine injury absence
  - healthy scratches and benchings
  - late-season rest once a team is eliminated

We cannot separate them from this feed, so the honest label is availability.
For a starter-quality player the dominant cause is injury, which is why the
number is still worth having - but a backup with low games played is usually a
depth-chart story, not a medical one. The read-out says so.

Base rates are computed from the data rather than asserted, so the familiar
claim that RBs get hurt more than QBs is something this module measures rather
than assumes.
"""

from __future__ import annotations

from . import sleeper

POSITIONS = ["QB", "RB", "WR", "TE"]

# The NFL moved to a 17-game season in 2021.
GAMES_PER_SEASON = 17
TTL = 7 * 24 * 3600


def _season_gp(season: str) -> dict[str, dict]:
    """{player_id: {position, games}} for one season."""
    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    url = (
        f"https://api.sleeper.app/stats/nfl/{season}"
        f"?season_type=regular&{qs}&order_by=pts_ppr"
    )
    try:
        rows = sleeper._get(url, f"stats_season_{season}", TTL)
    except Exception:
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        pid = r.get("player_id")
        pos = (r.get("player") or {}).get("position")
        stats = r.get("stats") or {}
        if not pid or pos not in POSITIONS:
            continue
        out[pid] = {
            "position": pos,
            "games": stats.get("gp") or 0,
            "points": stats.get("pts_ppr") or 0.0,
        }
    return out


def availability_table(seasons: list[str]) -> dict[str, dict]:
    """Multi-season availability per player.

    Sleeper returns a row for every player in every season queried, including
    seasons before he entered the league - those come back with 0 games. Left
    uncorrected they read as missed time, and a rookie looks like the most
    injury-prone player in football.

    So a career is anchored at the first season in which he actually appeared,
    and only seasons from that point forward are counted. A genuine lost season
    mid-career still counts against him, which is the behaviour we want.
    """
    per_season = {s: _season_gp(s) for s in seasons}
    ordered = sorted(seasons)

    players: dict[str, dict] = {}
    for season in ordered:
        for pid, rec in per_season[season].items():
            entry = players.setdefault(
                pid, {"position": rec["position"], "seasons": {}, "points": {}}
            )
            entry["seasons"][season] = rec["games"]
            entry["points"][season] = rec["points"]

    out: dict[str, dict] = {}
    for pid, entry in players.items():
        # Anchor on debut: the first season with any game played.
        debut = next(
            (s for s in ordered if entry["seasons"].get(s, 0) > 0), None
        )
        if debut is None:
            continue  # never played in the window; nothing to say

        counted = {
            s: entry["seasons"].get(s, 0) for s in ordered if s >= debut
        }
        total_games = sum(counted.values())
        possible = GAMES_PER_SEASON * len(counted)
        missed = possible - total_games

        out[pid] = {
            "position": entry["position"],
            "debut_season": debut,
            "seasons_tracked": len(counted),
            "games_by_season": counted,
            "games_played": total_games,
            "games_possible": possible,
            "games_missed": missed,
            "availability_pct": round(100 * total_games / possible, 1),
            "missed_per_season": round(missed / len(counted), 1),
            "full_seasons": sum(
                1 for g in counted.values() if g >= GAMES_PER_SEASON - 1
            ),
            "best_season_points": round(max(entry["points"].values() or [0]), 1),
        }

    return out


def position_base_rates(table: dict[str, dict], min_points: float = 100.0) -> dict:
    """Average games missed per season by position, among relevant players.

    The filter is doing real work: without it the sample is dominated by deep
    reserves who never dress, and the result measures depth-chart status rather
    than durability.
    """
    buckets: dict[str, list[float]] = {}
    for rec in table.values():
        if rec["seasons_tracked"] < 1:
            continue
        if rec.get("best_season_points", 0) < min_points:
            continue
        buckets.setdefault(rec["position"], []).append(rec["missed_per_season"])

    return {
        pos: {
            "avg_games_missed_per_season": round(sum(v) / len(v), 2),
            "sample": len(v),
        }
        for pos, v in sorted(buckets.items())
        if v
    }


def availability_read(
    rec: dict | None, base_rates: dict | None = None, age: int | None = None
) -> list[str]:
    """Plain-language durability summary, with the caveat built in."""
    if not rec:
        return ["no availability history (rookie, or no prior NFL games)"]

    notes: list[str] = []
    pct = rec["availability_pct"]
    per = rec["missed_per_season"]
    n = rec["seasons_tracked"]

    if pct >= 95:
        notes.append(f"iron man - {pct}% of games over {n} season(s)")
    elif pct >= 85:
        notes.append(f"generally available ({pct}% over {n} season(s))")
    elif pct >= 70:
        notes.append(
            f"missed time regularly - {per} games/season over {n} season(s)"
        )
    else:
        notes.append(
            f"major availability concern - only {pct}% of games over {n} season(s)"
        )

    if base_rates:
        base = (base_rates.get(rec["position"]) or {}).get(
            "avg_games_missed_per_season"
        )
        if base is not None:
            delta = per - base
            if delta >= 1.0:
                notes.append(
                    f"worse than the {rec['position']} average "
                    f"({per} vs {base} games missed/season)"
                )
            elif delta <= -0.75:
                # Phrasing matters when he misses time but the position misses
                # more - "more durable" alongside "missed time regularly" reads
                # as a contradiction rather than as context.
                lead = "still better than" if pct < 85 else "more durable than"
                notes.append(
                    f"{lead} the typical {rec['position']} ({per} vs {base})"
                )

    # The aging curve bites RBs first and hardest.
    if age is not None:
        if rec["position"] == "RB" and age >= 28:
            notes.append(f"age {age} at RB - past the point where decline usually starts")
        elif rec["position"] in ("WR", "TE") and age >= 31:
            notes.append(f"age {age} - in the decline band for the position")

    if n <= 1:
        notes.append(
            "one season of history only - too small a sample to judge durability"
        )

    if pct < 85:
        notes.append(
            "caveat: games played mixes injury with benchings and rest, so treat "
            "this as availability rather than a medical read"
        )

    return notes
