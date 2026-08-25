"""League-accurate scoring and value-over-replacement.

Sleeper is generous here: the keys in a league's `scoring_settings` are the
exact same keys used in the projected stat lines. So scoring a projection
against a specific league is a dot product, and we never have to fall back on
Sleeper's generic `pts_ppr` (which ignores whatever your league does
differently).
"""

from __future__ import annotations

from typing import Any

# Which real positions can fill each flex-ish slot.
FLEX_ELIGIBLE: dict[str, tuple[str, ...]] = {
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
    "IDP_FLEX": ("DL", "LB", "DB"),
}

# How a generic FLEX slot tends to get filled in practice. Used only to place
# replacement level, so it needs to be roughly right, not exact.
FLEX_SPLIT = {"RB": 0.45, "WR": 0.45, "TE": 0.10}
SUPERFLEX_SPLIT = {"QB": 0.7, "RB": 0.1, "WR": 0.15, "TE": 0.05}
IDP_SPLIT = {"DL": 0.34, "LB": 0.33, "DB": 0.33}

NON_PLAYER_SLOTS = {"BN", "IR", "TAXI"}


def score_stats(stats: dict[str, float], scoring: dict[str, float]) -> float:
    """Apply a league's scoring settings to one projected stat line."""
    if not stats:
        return 0.0
    total = 0.0
    for key, weight in scoring.items():
        value = stats.get(key)
        if value is None:
            continue
        # JSON does not guarantee numbers arrive as numbers - a stat or a
        # scoring weight can come back as a string. Coerce rather than crash,
        # and skip anything that genuinely is not numeric.
        try:
            total += float(value) * float(weight)
        except (TypeError, ValueError):
            continue
    return total


def _flex_shares(slot: str) -> dict[str, float]:
    """How one flex-type slot divides across the positions that can fill it."""
    if slot == "SUPER_FLEX":
        return dict(SUPERFLEX_SPLIT)

    eligible = FLEX_ELIGIBLE.get(slot, ())
    if not eligible:
        return {}

    # Prefer a configured split for the eligible set; fall back to spreading
    # evenly so a slot type this code has never seen still contributes.
    weights = {p: FLEX_SPLIT.get(p, IDP_SPLIT.get(p, 0.0)) for p in eligible}
    total = sum(weights.values())
    if total <= 0:
        return {p: 1.0 / len(eligible) for p in eligible}
    return {p: w / total for p, w in weights.items()}


def starter_counts(roster_positions: list[str], num_teams: int) -> dict[str, float]:
    """League-wide count of startable slots per real position.

    Every position the league actually starts is counted, derived from the
    roster shape rather than a fixed list. That matters beyond the skill
    positions: a league starting two defensive linemen, or no kicker at all,
    has to place replacement level accordingly. Assuming one starter per team
    for anything outside QB/RB/WR/TE gets both of those wrong.

    Flex slots are spread across the positions eligible to fill them, so a
    two-flex league correctly pushes RB/WR replacement deeper than a no-flex
    league would.
    """
    counts: dict[str, float] = {}

    for slot in roster_positions:
        if slot in NON_PLAYER_SLOTS:
            continue

        shares = _flex_shares(slot)
        if shares:
            for pos, share in shares.items():
                counts[pos] = counts.get(pos, 0.0) + share
            continue

        # A plain position slot - QB, RB, K, DEF, DL, LB, DB, or anything else
        # Sleeper introduces. One slot is one starter for that position.
        counts[slot] = counts.get(slot, 0.0) + 1

    return {pos: n * num_teams for pos, n in counts.items()}


def replacement_levels(
    players: list[dict[str, Any]],
    roster_positions: list[str],
    num_teams: int,
) -> dict[str, float]:
    """Projected points of the last startable player at each position.

    `players` must be dicts with at least "position" and "points".
    """
    baselines = starter_counts(roster_positions, num_teams)
    levels: dict[str, float] = {}

    by_pos: dict[str, list[float]] = {}
    for p in players:
        by_pos.setdefault(p["position"], []).append(p["points"])

    for pos, pts in by_pos.items():
        pts.sort(reverse=True)
        # A position the league does not start has no startable slots, so the
        # baseline sits at the best player available and every one of them
        # grades at or below zero - which is the correct answer for, say,
        # kickers in a league with no kicker slot. There is deliberately no
        # "assume one per team" fallback: that guessed a roster shape instead
        # of reading it, and was wrong for any league starting zero or two.
        starters = baselines.get(pos, 0.0)
        idx = max(0, int(round(starters)) - 1)
        levels[pos] = pts[min(idx, len(pts) - 1)] if pts else 0.0

    return levels


def build_player_values(
    projections: list[dict],
    scoring: dict[str, float],
    roster_positions: list[str],
    num_teams: int,
    adp_key: str = "adp_ppr",
) -> list[dict]:
    """Turn raw Sleeper projections into league-scored, VOR-ranked players.

    Returns a list sorted by value (VOR) descending.
    """
    rows: list[dict] = []
    for entry in projections:
        stats = entry.get("stats") or {}
        meta = entry.get("player") or {}
        pos = meta.get("position") or (meta.get("fantasy_positions") or [None])[0]
        if not pos:
            continue

        points = score_stats(stats, scoring)
        if points <= 0:
            continue

        adp = stats.get(adp_key)
        # Sleeper uses 999/1000 as "undrafted" sentinels.
        if adp is None or adp >= 400:
            adp = None

        name = f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
        rows.append(
            {
                "player_id": entry.get("player_id"),
                "name": name or meta.get("last_name") or "Unknown",
                "position": pos,
                "team": meta.get("team"),
                "points": round(points, 1),
                "adp": adp,
                "games": stats.get("gp"),
                "bye": meta.get("bye_week"),
                "injury_status": meta.get("injury_status"),
                "years_exp": meta.get("years_exp"),
            }
        )

    levels = replacement_levels(rows, roster_positions, num_teams)
    for r in rows:
        r["replacement"] = round(levels.get(r["position"], 0.0), 1)
        r["vor"] = round(r["points"] - r["replacement"], 1)

    rows.sort(key=lambda r: r["vor"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["value_rank"] = i

    return rows


def positional_ranks(rows: list[dict]) -> None:
    """Annotate each row with its rank inside its own position (RB1, WR14...)."""
    counters: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: r["points"], reverse=True):
        pos = r["position"]
        counters[pos] = counters.get(pos, 0) + 1
        r["pos_rank"] = f"{pos}{counters[pos]}"
