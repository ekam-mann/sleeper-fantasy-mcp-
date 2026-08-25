"""Recent form - what changed lately, separated from what merely happened.

Everything else in this codebase aggregates a whole season. That hides the
question you actually ask in November: is his role growing or shrinking?

Two things get called "form" and they are not equally useful:

  - **Production trend** (points per game rising or falling). Mostly noise.
    It is dominated by touchdowns, which regress hard, so a hot three-week
    stretch is usually luck wearing a costume.
  - **Opportunity trend** (targets, carries, snaps, red-zone looks). Sticky.
    A back whose carry share climbed from 40% to 65% has a genuinely different
    job than he had a month ago, and that persists.

So this module leads with opportunity and reports production second, explicitly
labelled as the noisier signal. A module that ranked by points trend would look
more exciting and would mostly be recommending touchdown luck.

Two sampling decisions that matter more than they look:

  - Windows are the last N games a player **actually played**, not the last N
    calendar weeks. Otherwise a player returning from injury shows a collapsed
    role when what really happened is that he was not on the field.
  - The comparison is against the **previous N played games**, not against his
    season average. Equal sample sizes make the delta a like-for-like
    comparison rather than "recent versus a number that includes recent".
"""

from __future__ import annotations

from . import memo, sleeper
from . import scoring as scoring_mod

POSITIONS = ["QB", "RB", "WR", "TE"]
TTL = 7 * 24 * 3600

# Below this many games in *each* window there is nothing to compare.
MIN_GAMES_PER_WINDOW = 2

# The 17-game era. Per-game share maths is calibrated on it, and anything
# before it would need a different games-per-season constant.
FIRST_SUPPORTED_SEASON = 2021
# Generous upper bound: guards typos like 20226 without pinning to a year.
LAST_PLAUSIBLE_SEASON = 2100


def _weekly_rows(season: str) -> list[dict]:
    """Every weekly stat line for the season, cached alongside the SOS build."""
    # Reject an implausible season before issuing eighteen requests for it.
    # Asking for 1990 previously cost 777 seconds of timeouts to learn what
    # arithmetic answers instantly, and the 17-game era is the only period this
    # module's per-game maths is calibrated for anyway.
    try:
        year = int(str(season))
    except (TypeError, ValueError):
        raise ValueError(f"Season must be a year, got {season!r}") from None
    if not (FIRST_SUPPORTED_SEASON <= year <= LAST_PLAUSIBLE_SEASON):
        raise ValueError(
            f"Season {year} is outside the supported range "
            f"({FIRST_SUPPORTED_SEASON}-{LAST_PLAUSIBLE_SEASON})."
        )

    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    rows: list[dict] = []
    failures = 0
    for week in range(1, 19):
        url = (
            f"https://api.sleeper.app/stats/nfl/{season}/{week}"
            f"?season_type=regular&{qs}&order_by=pts_ppr"
        )
        try:
            rows.extend(sleeper._get(url, f"stats_{season}_wk{week}", TTL))
        except Exception:
            failures += 1
            continue

    # Individual weeks legitimately come back empty - a season in progress has
    # no week 15 yet. Every week failing is a different thing entirely: a bad
    # season identifier, or the API being unreachable. Returning [] there hands
    # back an empty table that reads exactly like "nobody qualified", which is
    # how a shadowed variable once passed a dict in here and produced a
    # confident, empty answer instead of an error.
    if not rows:
        raise ValueError(
            f"No weekly stats for season {season!r} "
            f"({failures}/18 requests failed). Refusing to return an empty "
            "form table that would look like a valid result."
        )
    return rows


def _row_team(r: dict) -> str | None:
    """The team a player actually suited up for in THIS week.

    Not `player.team`, which is his *current* roster team. For a free agent it
    is null, and for anyone who changed clubs it names the wrong side - so
    using it silently drops some players' shares and computes the rest against
    another team's denominator. The row-level `team` is populated on every
    weekly line and is the team that played the game.
    """
    return r.get("team") or (r.get("player") or {}).get("team")


def _team_week_totals(rows: list[dict]) -> dict[tuple[str, int], dict[str, float]]:
    """Team targets and carries per week, for weekly share denominators."""
    totals: dict[tuple[str, int], dict[str, float]] = {}
    for r in rows:
        team = _row_team(r)
        week = r.get("week")
        s = r.get("stats") or {}
        if not team or week is None:
            continue
        key = (team, week)
        bucket = totals.setdefault(key, {"targets": 0.0, "carries": 0.0})
        bucket["targets"] += s.get("rec_tgt") or 0
        bucket["carries"] += s.get("rush_att") or 0
    return totals


def _game_metrics(
    r: dict,
    team_totals: dict[tuple[str, int], dict[str, float]],
    scoring: dict[str, float] | None,
) -> dict | None:
    """One game's worth of opportunity and production."""
    s = r.get("stats") or {}
    if not s.get("gp"):
        return None  # did not dress - an absence, not a zero

    team = _row_team(r)
    week = r.get("week")
    tt = team_totals.get((team, week)) or {}

    targets = s.get("rec_tgt") or 0.0
    carries = s.get("rush_att") or 0.0

    def share(part: float, whole: float | None) -> float | None:
        """A share of a whole, capped at 100%.

        The cap is not cosmetic. Sleeper occasionally reports a player with more
        snaps than his own team - one 2025 quarterback has off_snp=62 against
        tm_off_snp=61 in week 17, giving 101.6%. It is an off-by-one in the
        source, not in this arithmetic, but a share above 100% is impossible and
        must never reach a caller who reasonably assumes shares are shares.

        It went unnoticed until the form windows were made symmetric, because
        the player only has five games and the old asymmetric slicing dropped
        everyone with fewer than eight.
        """
        if not whole:
            return None
        return min(100.0, 100.0 * part / whole)

    # Sleeper omits pts_ppr entirely for a player who dressed and produced
    # nothing, rather than writing 0. Those are real zero-point games.
    if scoring:
        points = scoring_mod.score_stats(s, scoring)
    else:
        points = s.get("pts_ppr") or 0.0

    return {
        "week": week,
        "points": points,
        "targets": targets,
        "carries": carries,
        "opportunities": targets + carries,
        "target_share": share(targets, tt.get("targets")),
        "carry_share": share(carries, tt.get("carries")),
        "snap_share": share(s.get("off_snp") or 0.0, s.get("tm_off_snp")),
        "rz_looks": (s.get("rush_rz_att") or 0.0) + (s.get("rec_rz_tgt") or 0.0),
    }


# A game at less than this fraction of a player's own usual snap share is
# treated as shortened - he left early or was limited, rather than demoted.
SHORTENED_SNAP_RATIO = 0.6


def _shortened_games(games: list[dict], baseline_snap: float | None) -> list[int]:
    """Weeks in this window where he played far below his own norm.

    These are kept in the averages - they really happened - but they are
    reported, because one half-game inside a four-game window can look
    identical to a genuine loss of role and is a completely different thing.
    """
    if not baseline_snap:
        return []
    return [
        g["week"]
        for g in games
        if g.get("snap_share") is not None
        and g["snap_share"] < baseline_snap * SHORTENED_SNAP_RATIO
    ]


def _median(values: list[float | None]) -> float | None:
    present = sorted(v for v in values if v is not None)
    if not present:
        return None
    mid = len(present) // 2
    if len(present) % 2:
        return present[mid]
    return (present[mid - 1] + present[mid]) / 2


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _summarise(games: list[dict]) -> dict:
    return {
        "games": len(games),
        "weeks": [g["week"] for g in games],
        "ppg": _mean([g["points"] for g in games]),
        "opportunities_per_game": _mean([g["opportunities"] for g in games]),
        "target_share": _mean([g["target_share"] for g in games]),
        "carry_share": _mean([g["carry_share"] for g in games]),
        "snap_share": _mean([g["snap_share"] for g in games]),
        "rz_looks_per_game": _mean([g["rz_looks"] for g in games]),
    }


def _delta(recent: float | None, prior: float | None) -> float | None:
    if recent is None or prior is None:
        return None
    return recent - prior


@memo.table
def form_table(
    season: str,
    scoring: dict[str, float] | None = None,
    window: int = 4,
) -> dict[str, dict]:
    """Recent versus prior form for every player. Returns {player_id: {...}}.

    `window` is a number of games played, not calendar weeks.
    """
    rows = _weekly_rows(season)
    team_totals = _team_week_totals(rows)

    by_player: dict[str, dict] = {}
    for r in rows:
        pid = r.get("player_id")
        pos = (r.get("player") or {}).get("position")
        if not pid or pos not in POSITIONS:
            continue
        g = _game_metrics(r, team_totals, scoring)
        if g is None:
            continue
        entry = by_player.setdefault(pid, {"position": pos, "games": []})
        entry["games"].append(g)

    out: dict[str, dict] = {}
    for pid, entry in by_player.items():
        games = sorted(entry["games"], key=lambda g: g["week"])
        if len(games) < MIN_GAMES_PER_WINDOW * 2:
            continue

        # Both windows must be the SAME length, or the comparison is not a
        # comparison. Slicing a fixed `window` off each end silently produces
        # unequal halves for anyone with fewer than 2*window games: at window=4
        # a six-game player is scored as his last four against his first *two*,
        # so the prior side carries twice the sampling noise and every delta
        # inherits it. Players in that 4-7 game range are exactly the ones a
        # form read gets asked about - returning from injury, or just promoted.
        #
        # Halving to the games actually available keeps the two sides
        # symmetric, at the cost of a shorter window than requested. The window
        # actually used is reported rather than the one asked for.
        effective_window = min(window, len(games) // 2)
        if effective_window < MIN_GAMES_PER_WINDOW:
            continue

        recent_games = games[-effective_window:]
        prior_games = games[-2 * effective_window : -effective_window]

        recent = _summarise(recent_games)
        prior = _summarise(prior_games)

        # Baseline is his own median snap share across the whole season, so a
        # rotational player is not flagged simply for being rotational.
        baseline_snap = _median([g.get("snap_share") for g in games])
        recent["shortened_weeks"] = _shortened_games(recent_games, baseline_snap)
        prior["shortened_weeks"] = _shortened_games(prior_games, baseline_snap)

        out[pid] = {
            "season": season,
            "position": entry["position"],
            "window": effective_window,
            "window_requested": window,
            "baseline_snap_share": baseline_snap,
            "recent": recent,
            "prior": prior,
            "delta": {
                "ppg": _delta(recent["ppg"], prior["ppg"]),
                "opportunities_per_game": _delta(
                    recent["opportunities_per_game"], prior["opportunities_per_game"]
                ),
                "target_share": _delta(recent["target_share"], prior["target_share"]),
                "carry_share": _delta(recent["carry_share"], prior["carry_share"]),
                "snap_share": _delta(recent["snap_share"], prior["snap_share"]),
                "rz_looks_per_game": _delta(
                    recent["rz_looks_per_game"], prior["rz_looks_per_game"]
                ),
            },
        }

    return out


def opportunity_trend_score(f: dict | None) -> float | None:
    """A single number for whether the ROLE is growing. Opportunity only.

    Deliberately excludes points. Mixing production into a "trend" score is how
    you end up recommending a player whose only change was three touchdowns.
    """
    if not f:
        return None
    d = f["delta"]
    parts = [
        (d.get("snap_share"), 1.0),
        (d.get("target_share"), 1.5),
        (d.get("carry_share"), 1.0),
        (d.get("rz_looks_per_game"), 3.0),  # few per game, so weight each heavily
    ]
    present = [(v, w) for v, w in parts if v is not None]
    if not present:
        return None
    return round(sum(v * w for v, w in present) / sum(w for _, w in present), 2)


def form_read(f: dict | None, position: str) -> list[str]:
    """Plain-language read, opportunity first and production flagged as noisy."""
    if not f:
        return ["not enough games played to judge form"]

    notes: list[str] = []
    d = f["delta"]
    r, p = f["recent"], f["prior"]

    short = r.get("shortened_weeks") or []
    if short:
        weeks = ", ".join(f"wk{w}" for w in short)
        notes.append(
            f"CAVEAT: {weeks} played well below his usual snaps "
            f"(~{f.get('baseline_snap_share') or 0:.0f}%) - a shortened game is "
            "inside this window, so the drop overstates any real role change"
        )

    snap = d.get("snap_share")
    if snap is not None:
        if snap >= 10:
            arrived = (
                "now an every-down player"
                if (r.get("snap_share") or 0) >= 70
                else "the role is growing, though still rotational"
                if (r.get("snap_share") or 0) < 45
                else "the role is growing"
            )
            notes.append(
                f"snap share up {snap:+.0f} pts ({p['snap_share']:.0f}% -> "
                f"{r['snap_share']:.0f}%) - {arrived}"
            )
        elif snap <= -10:
            notes.append(
                f"snap share down {snap:+.0f} pts ({p['snap_share']:.0f}% -> "
                f"{r['snap_share']:.0f}%) - losing playing time"
            )

    # A delta means nothing without the level it moved to. Going from 7% to
    # 20% of snaps and from 60% to 73% are the same +13, and only one of them
    # is a starter. Every claim below is therefore gated on where he ended up.
    if position in ("WR", "TE") and d.get("target_share") is not None:
        ts, now = d["target_share"], r.get("target_share") or 0
        if ts >= 5:
            if now >= 20:
                notes.append(
                    f"target share up {ts:+.0f} pts to {now:.0f}% - a primary option now"
                )
            else:
                notes.append(
                    f"target share up {ts:+.0f} pts, but only to {now:.0f}% - "
                    "more involved, still not a focal point"
                )
        elif ts <= -5:
            notes.append(
                f"target share down {ts:+.0f} pts to {now:.0f}% - falling out of the plan"
            )

    if position == "RB" and d.get("carry_share") is not None:
        cs, now = d["carry_share"], r.get("carry_share") or 0
        snap_now = r.get("snap_share") or 0
        if cs >= 10:
            if now >= 50 and snap_now >= 40:
                notes.append(
                    f"carry share up {cs:+.0f} pts to {now:.0f}% on {snap_now:.0f}% "
                    "of snaps - he is the lead back now"
                )
            else:
                notes.append(
                    f"carry share up {cs:+.0f} pts to {now:.0f}%, but on only "
                    f"{snap_now:.0f}% of snaps - a bigger slice of a rotational role, "
                    "not a takeover"
                )
        elif cs <= -10:
            notes.append(
                f"carry share down {cs:+.0f} pts to {now:.0f}% - ceding carries"
            )

    rz = d.get("rz_looks_per_game")
    if rz is not None and abs(rz) >= 1.0:
        notes.append(
            f"red-zone looks {rz:+.1f}/game - scoring chances are "
            f"{'up' if rz > 0 else 'down'}"
        )

    ppg = d.get("ppg")
    if ppg is not None and abs(ppg) >= 4:
        opp = d.get("opportunities_per_game")
        if opp is not None and abs(opp) < 2:
            notes.append(
                f"scoring {ppg:+.1f} ppg on flat usage - efficiency or touchdown "
                "luck, expect it to regress"
            )
        else:
            notes.append(f"scoring {ppg:+.1f} ppg, backed by the usage change")

    if not notes:
        notes.append("steady - no meaningful change in role or production")

    notes.append(
        f"last {r['games']} games vs previous {p['games']}: "
        f"{r['opportunities_per_game']:.1f} vs {p['opportunities_per_game']:.1f} "
        "opportunities/game"
    )
    return notes
