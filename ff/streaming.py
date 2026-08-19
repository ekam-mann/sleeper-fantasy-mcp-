"""Streaming - picking a starter by matchup rather than by name.

Streaming works where matchup variance is large relative to talent variance.
That is emphatically true at defense, mostly true at kicker, and true at QB and
TE only in the shallow part of the pool - nobody streams an elite tight end.

Two different signals drive it, and they point in opposite directions:

  - **For a defense**, you want the opponent's implied team total to be LOW.
    A defense facing an offense implied for 17 is in a far better spot than one
    facing an offense implied for 27, and that gap dwarfs the difference
    between the defenses themselves.
  - **For an offensive player**, you want his own team's implied total HIGH,
    and you want the opposing defense to be generous to his position.

Getting that inversion backwards is the classic way to stream badly, so the two
paths are kept deliberately separate below.
"""

from __future__ import annotations

from . import sos, vegas

OFFENSIVE = {"QB", "TE", "K", "RB", "WR"}


# --------------------------------------------------------------------------
# Shared: pull each team's implied total and opponent for the week
# --------------------------------------------------------------------------


def _week_context(season: str, week: int) -> tuple[dict, dict]:
    """(implied_total_by_team, opponent_by_team) for a week."""
    implied: dict[str, float] = {}
    opponent: dict[str, str] = {}
    for g in vegas.week_odds(season, week):
        totals = g.get("implied_totals") or {}
        if not totals:
            continue
        home, away = g["home"], g["away"]
        opponent[home], opponent[away] = away, home
        implied.update(totals)
    return implied, opponent


# --------------------------------------------------------------------------
# Defense
# --------------------------------------------------------------------------


def _grade_defense(opp_implied: float | None) -> tuple[int, str]:
    if opp_implied is None:
        return 0, "no line posted"
    if opp_implied <= 17.5:
        return 3, "elite streaming spot - opponent implied under 17.5"
    if opp_implied <= 20.5:
        return 2, "strong streaming spot"
    if opp_implied <= 23.5:
        return 1, "playable"
    return 0, "avoid - opponent expected to score freely"


def defense_streamers(
    season: str,
    week: int,
    rows: list[dict],
    available: set[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Rank defenses by matchup. Lower opponent implied total is better."""
    implied, opponent = _week_context(season, week)

    out: list[dict] = []
    for d in (r for r in rows if r.get("position") == "DEF"):
        team = d.get("team") or d.get("player_id")
        if available is not None and d.get("player_id") not in available:
            continue
        opp = opponent.get(team)
        opp_implied = implied.get(opp) if opp else None
        score, verdict = _grade_defense(opp_implied)
        out.append(
            {
                "player": d.get("name") or team,
                "team": team,
                "opponent": opp,
                "opponent_implied_total": opp_implied,
                "projected_points": d.get("points"),
                "matchup_score": score,
                "verdict": verdict,
            }
        )

    out.sort(
        key=lambda x: (x["matchup_score"], x.get("projected_points") or 0), reverse=True
    )
    return out[:limit]


# --------------------------------------------------------------------------
# Offensive positions
# --------------------------------------------------------------------------


def _grade_offense(
    own_implied: float | None, def_rank: int | None
) -> tuple[int, list[str]]:
    """Combine scoring environment with opponent generosity to the position.

    `def_rank` is 1-32 where higher means the defense allows more points to
    that position, i.e. a better matchup to face.
    """
    score = 0
    notes: list[str] = []

    if own_implied is not None:
        if own_implied >= 26:
            score += 2
            notes.append(f"high-scoring spot ({own_implied} implied)")
        elif own_implied >= 22:
            score += 1
            notes.append(f"decent scoring spot ({own_implied} implied)")
        elif own_implied <= 18:
            score -= 1
            notes.append(f"low-scoring spot ({own_implied} implied)")

    if def_rank is not None:
        if def_rank >= 24:
            score += 2
            notes.append(f"faces a generous defense (rank {def_rank}/32 vs the position)")
        elif def_rank >= 18:
            score += 1
            notes.append(f"decent positional matchup (rank {def_rank}/32)")
        elif def_rank <= 8:
            score -= 1
            notes.append(f"tough positional matchup (rank {def_rank}/32)")

    return score, notes


def position_streamers(
    position: str,
    season: str,
    prior_season: str,
    week: int,
    rows: list[dict],
    available: set[str] | None = None,
    limit: int = 10,
    max_projection: float | None = None,
    starters_only: bool = True,
    scoring: dict[str, float] | None = None,
) -> list[dict]:
    """Rank streamable options at an offensive position for one week.

    `max_projection` filters out players nobody would stream - there is no
    point recommending you stream a top-five tight end you do not own.

    `starters_only` matters more than it looks. A backup quarterback inherits
    his team's matchup on paper, so without this filter the rankings fill with
    QB2s who have a lovely matchup and will not take a snap.
    """
    pos = position.upper()
    implied, opponent = _week_context(season, week)

    # Kicker has no per-position defensive split in the points-allowed data,
    # so it leans entirely on the scoring environment.
    ranks = {}
    if pos in ("QB", "TE", "RB", "WR"):
        try:
            ranks = sos.defense_ranks(prior_season, scoring)
        except Exception:
            ranks = {}

    out: list[dict] = []
    for p in (r for r in rows if r.get("position") == pos):
        if available is not None and p.get("player_id") not in available:
            continue
        if max_projection is not None and (p.get("points") or 0) > max_projection:
            continue
        if starters_only:
            order = p.get("depth_chart_order")
            # QB and K are strictly one-man jobs; skip anyone not atop the chart.
            if pos in ("QB", "K") and order is not None and order > 1:
                continue
            if pos in ("TE", "RB", "WR") and order is not None and order > 2:
                continue

        team = p.get("team")
        opp = opponent.get(team) if team else None
        own_implied = implied.get(team) if team else None
        def_rank = (ranks.get(opp) or {}).get(pos) if opp else None

        score, notes = _grade_offense(own_implied, def_rank)
        out.append(
            {
                "player": p.get("name"),
                "team": team,
                "opponent": opp,
                "team_implied_total": own_implied,
                "opponent_rank_vs_position": def_rank,
                "projected_points": p.get("points"),
                "matchup_score": score,
                "notes": notes,
            }
        )

    out.sort(
        key=lambda x: (x["matchup_score"], x.get("projected_points") or 0), reverse=True
    )
    return out[:limit]


def streaming_note(position: str, has_lines: bool) -> str:
    if not has_lines:
        return (
            "No betting lines posted for this week yet, so matchup grades are "
            "unavailable. Lines usually appear the Sunday or Monday prior."
        )
    if position.upper() == "DEF":
        return (
            "Ranked by opponent implied total (lower is better for a defense). "
            "Matchup outranks season projection here by design."
        )
    if position.upper() == "K":
        return (
            "Kickers key off their own team's scoring environment; there is no "
            "reliable per-position defensive split for kicking."
        )
    return (
        "Combines the player's own scoring environment with how generous the "
        "opposing defense was to this position last season."
    )
