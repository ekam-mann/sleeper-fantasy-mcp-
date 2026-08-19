"""Strength of schedule, built from actual fantasy points allowed.

Two pieces have to come together:

  1. How generous each defense is to each position. Derived from last season's
     weekly stats, which carry an `opponent` field — so we can total the PPR
     points every defense surrendered to QB/RB/WR/TE and rank them.
  2. Who each team plays, and when. ESPN's scoreboard gives the matchups.

The fantasy-relevant question is rarely "whose season schedule is easiest" —
that mostly evens out. It's "who is set up for weeks 15-17," when your league
is decided. So playoff SOS is reported separately and weighted heavier.
"""

from __future__ import annotations

import hashlib
import json
import json as _json
import time
from pathlib import Path

import httpx

from . import scoring as scoring_mod
from . import sleeper

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
TTL = 7 * 24 * 3600

POSITIONS = ["QB", "RB", "WR", "TE"]
PLAYOFF_WEEKS = (15, 16, 17)

TEAM_FIXUPS = {"WSH": "WAS"}


def _fix(t: str | None) -> str | None:
    return TEAM_FIXUPS.get(t, t) if t else None


# --------------------------------------------------------------------------
# 1. Defensive generosity
# --------------------------------------------------------------------------


def _scoring_key(scoring: dict[str, float] | None) -> str:
    """Short stable digest of a scoring config, for cache separation."""
    if not scoring:
        return "ppr"
    blob = _json.dumps(scoring, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:10]


def points_allowed(
    season: str, scoring: dict[str, float] | None = None
) -> dict[str, dict[str, float]]:
    """Points allowed per game by each defense, split by position.

    Scored through the league's own settings when supplied. Defensive ranks are
    relative, so this rarely reorders much in a standard league - but under
    TE-premium or six-point passing TDs the ordering genuinely shifts, and the
    cache is keyed by scoring so two leagues never share the wrong table.
    """
    path = CACHE_DIR / f"pts_allowed_{season}_{_scoring_key(scoring)}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < TTL:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    totals: dict[str, dict[str, float]] = {}
    games: dict[str, set] = {}

    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    for week in range(1, 19):
        url = (
            f"https://api.sleeper.app/stats/nfl/{season}/{week}"
            f"?season_type=regular&{qs}&order_by=pts_ppr"
        )
        try:
            rows = sleeper._get(url, f"stats_{season}_wk{week}", TTL)
        except Exception:
            continue

        for r in rows:
            opp = _fix(r.get("opponent"))
            pos = (r.get("player") or {}).get("position")
            stats = r.get("stats") or {}
            if scoring:
                pts = (
                    scoring_mod.score_stats(stats, scoring) if stats.get("gp") else None
                )
            else:
                pts = stats.get("pts_ppr")
            if not opp or pos not in POSITIONS or pts is None:
                continue
            totals.setdefault(opp, {}).setdefault(pos, 0.0)
            totals[opp][pos] += pts
            games.setdefault(opp, set()).add(week)

    per_game = {
        team: {
            pos: round(pts / max(len(games[team]), 1), 2)
            for pos, pts in by_pos.items()
        }
        for team, by_pos in totals.items()
    }

    if len(per_game) >= 30:
        path.write_text(json.dumps(per_game), encoding="utf-8")
    return per_game


def defense_ranks(
    season: str, scoring: dict[str, float] | None = None
) -> dict[str, dict[str, int]]:
    """Rank 1-32 per position. Rank 1 = most generous (best matchup to face)."""
    pa = points_allowed(season, scoring)
    ranks: dict[str, dict[str, int]] = {t: {} for t in pa}
    for pos in POSITIONS:
        ordered = sorted(
            (t for t in pa if pos in pa[t]),
            key=lambda t: pa[t][pos],
            reverse=True,  # most points allowed first
        )
        for i, team in enumerate(ordered, start=1):
            ranks[team][pos] = i
    return ranks


# --------------------------------------------------------------------------
# 2. Who plays whom
# --------------------------------------------------------------------------


def team_schedule(season: str) -> dict[str, dict[int, str]]:
    """{team: {week: opponent}} for the regular season."""
    path = CACHE_DIR / f"schedule_{season}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < TTL:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {t: {int(w): o for w, o in wk.items()} for t, wk in raw.items()}
        except (json.JSONDecodeError, ValueError):
            pass

    sched: dict[str, dict[int, str]] = {}
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for week in range(1, 19):
            try:
                resp = client.get(
                    ESPN, params={"dates": season, "seasontype": 2, "week": week}
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue

            for event in data.get("events", []):
                for comp in event.get("competitions", []):
                    teams = [
                        _fix((c.get("team") or {}).get("abbreviation"))
                        for c in comp.get("competitors", [])
                    ]
                    if len(teams) == 2 and all(teams):
                        a, b = teams
                        sched.setdefault(a, {})[week] = b
                        sched.setdefault(b, {})[week] = a

    if len(sched) >= 30:
        path.write_text(
            json.dumps({t: {str(w): o for w, o in wk.items()} for t, wk in sched.items()}),
            encoding="utf-8",
        )
    return sched


# --------------------------------------------------------------------------
# 3. Put them together
# --------------------------------------------------------------------------


def player_sos(
    team: str,
    position: str,
    season: str,
    prior_season: str,
    scoring: dict[str, float] | None = None,
) -> dict | None:
    """Schedule difficulty for one team/position.

    `season` is the upcoming season (whose schedule we read); `prior_season`
    supplies the defensive grades, since the new one hasn't been played yet.
    """
    if not team or position not in POSITIONS:
        return None

    sched = team_schedule(season).get(team)
    ranks = defense_ranks(prior_season, scoring)
    if not sched:
        return None

    def _avg(weeks) -> float | None:
        vals = [
            ranks[opp][position]
            for w in weeks
            if (opp := sched.get(w)) and opp in ranks and position in ranks[opp]
        ]
        return round(sum(vals) / len(vals), 1) if vals else None

    full = _avg(range(1, 19))
    playoff = _avg(PLAYOFF_WEEKS)

    return {
        "team": team,
        "position": position,
        "season_sos_rank": full,
        "playoff_sos_rank": playoff,
        "playoff_matchups": {
            w: sched.get(w) for w in PLAYOFF_WEEKS if sched.get(w)
        },
        "verdict": _verdict(playoff),
        # Ranks are "opponent generosity": higher = softer defenses faced.
        "scale": "1-32 avg opponent rank; higher = easier (more points allowed)",
    }


def _verdict(playoff: float | None) -> str:
    if playoff is None:
        return "unknown"
    if playoff >= 21:
        return "soft playoff slate — a real tiebreaker in his favor"
    if playoff <= 12:
        return "brutal playoff slate — discount him slightly"
    return "neutral playoff slate"
