"""Week-to-week consistency: floor, ceiling, and boom/bust profile.

A season projection is a mean. Two players with the same mean can be very
different assets: one gives you 14 points every week, the other gives you 30
twice and 4 the rest of the time. Which you want depends on the format.

  - Weekly head-to-head: floor usually wins. You start him every week, so a
    zero is a loss you cannot undo.
  - Best ball / tournaments: ceiling wins. Only his good weeks get counted.

Boom and bust thresholds are derived from the data rather than hardcoded,
because 18 points means something different at TE than at RB. For each
position we take the weekly scores of players who actually finished as
startable, and read the percentiles off that distribution.
"""

from __future__ import annotations

import statistics

from . import memo, sleeper
from . import scoring as scoring_mod

POSITIONS = ["QB", "RB", "WR", "TE"]

# Fallback only, for callers with no league to derive from. The real values
# come from the league's own roster shape and team count via
# `scoring.starter_counts`. A fixed table would set the boom/bust bar at the
# height of whichever league it was written against, and at the wrong height
# for every other one.
DEFAULT_STARTABLE = {"QB": 14, "RB": 36, "WR": 42, "TE": 14}

TTL = 7 * 24 * 3600


def _weekly_rows(season: str) -> list[dict]:
    """Every weekly stat line for the season. Cached by the SOS build already."""
    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    rows: list[dict] = []
    for week in range(1, 19):
        url = (
            f"https://api.sleeper.app/stats/nfl/{season}/{week}"
            f"?season_type=regular&{qs}&order_by=pts_ppr"
        )
        try:
            rows.extend(sleeper._get(url, f"stats_{season}_wk{week}", TTL))
        except Exception:
            continue
    return rows


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile. Avoids a numpy dependency."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _thresholds(
    by_player: dict[str, dict], startable: dict[str, float] | None = None
) -> dict[str, dict[str, float]]:
    """Per-position boom/bust cutoffs, read off the startable player pool.

    `startable` is how many players at each position the league actually starts
    league-wide - roster shape times team count. It sets the reference pool, so
    reading it from the league rather than assuming a size is what makes the
    boom/bust bar mean the same thing regardless of league size: fewer teams
    means a shallower startable pool and a correspondingly higher bar.
    """
    startable = startable or DEFAULT_STARTABLE
    out: dict[str, dict[str, float]] = {}
    for pos in POSITIONS:
        pool = [p for p in by_player.values() if p["position"] == pos and p["scores"]]
        pool.sort(key=lambda p: sum(p["scores"]), reverse=True)
        pool = pool[: max(1, int(round(startable.get(pos, 24))))]

        scores = [s for p in pool for s in p["scores"]]
        if not scores:
            continue
        out[pos] = {
            "boom": round(_percentile(scores, 0.80), 1),
            "bust": round(_percentile(scores, 0.20), 1),
        }
    return out


@memo.table
def volatility_table(
    season: str,
    scoring: dict[str, float] | None = None,
    startable: dict[str, float] | None = None,
) -> dict[str, dict]:
    """Per-player consistency profile. Returns {player_id: {...}}.

    `scoring` is the league's own scoring_settings. Without it we fall back to
    Sleeper's pre-computed PPR total, which is only correct for a full-PPR
    league - in a half-PPR or TE-premium league the floor and ceiling numbers
    would silently describe a different game than the one being played.
    """
    by_player: dict[str, dict] = {}

    for r in _weekly_rows(season):
        pid = r.get("player_id")
        pos = (r.get("player") or {}).get("position")
        stats = r.get("stats") or {}
        # Sleeper omits pts_ppr entirely for a player who dressed but produced
        # nothing, rather than writing 0. Those are real zero-point weeks and
        # they belong in a floor calculation - dropping them would quietly
        # inflate the floor of every low-usage player. Both branches below must
        # treat them identically, so the only thing that differs between a
        # scored and an unscored run is the scoring itself.
        if not stats.get("gp"):
            pts = None  # did not dress; not a zero, an absence
        elif scoring:
            pts = scoring_mod.score_stats(stats, scoring)
        else:
            pts = stats.get("pts_ppr") or 0.0
        if not pid or pos not in POSITIONS or pts is None:
            continue
        entry = by_player.setdefault(pid, {"position": pos, "scores": []})
        entry["scores"].append(pts)

    cutoffs = _thresholds(by_player, startable)
    out: dict[str, dict] = {}

    for pid, entry in by_player.items():
        scores = entry["scores"]
        pos = entry["position"]
        # Fewer than four games is too little to say anything about variance.
        if len(scores) < 4:
            continue

        mean = statistics.mean(scores)
        sd = statistics.pstdev(scores)
        cut = cutoffs.get(pos, {})
        boom_line, bust_line = cut.get("boom"), cut.get("bust")

        booms = sum(1 for s in scores if boom_line is not None and s >= boom_line)
        busts = sum(1 for s in scores if bust_line is not None and s <= bust_line)

        out[pid] = {
            "season": season,
            "games": len(scores),
            "mean": round(mean, 1),
            "median": round(statistics.median(scores), 1),
            "floor": round(_percentile(scores, 0.10), 1),
            "ceiling": round(_percentile(scores, 0.90), 1),
            "best": round(max(scores), 1),
            "worst": round(min(scores), 1),
            "std_dev": round(sd, 1),
            # Coefficient of variation makes spread comparable across players
            # with different means. Lower = steadier.
            "cv": round(sd / mean, 2) if mean > 0 else None,
            "boom_rate": round(100 * booms / len(scores)),
            "bust_rate": round(100 * busts / len(scores)),
            "boom_line": boom_line,
            "bust_line": bust_line,
        }

    return out


def consistency_read(v: dict | None, position: str) -> list[str]:
    """Plain-language summary of a volatility profile."""
    if not v:
        return ["no weekly history (rookie, or too few games)"]

    notes: list[str] = []
    cv = v.get("cv")
    if cv is not None:
        if cv <= 0.45:
            notes.append(f"steady week to week (CV {cv}) — a floor play")
        elif cv >= 0.75:
            notes.append(f"highly volatile (CV {cv}) — boom/bust")

    boom, bust = v.get("boom_rate"), v.get("bust_rate")
    if boom is not None and boom >= 35:
        notes.append(f"league-winning ceiling — boomed in {boom}% of games")
    if bust is not None and bust >= 35:
        notes.append(f"dangerous floor — busted in {bust}% of games")
    if boom is not None and bust is not None and boom >= 25 and bust <= 15:
        notes.append("rare profile: high ceiling without the matching downside")

    notes.append(
        f"floor {v['floor']} / median {v['median']} / ceiling {v['ceiling']} PPR"
    )
    return notes


def format_fit(v: dict | None) -> str:
    """Which format this player's shape suits."""
    if not v or v.get("cv") is None:
        return "unknown"
    cv = v["cv"]
    if cv <= 0.45:
        return "best in weekly head-to-head — you can set and forget him"
    if cv >= 0.75:
        return "best in best-ball; risky as a weekly must-start"
    return "no strong format preference"
