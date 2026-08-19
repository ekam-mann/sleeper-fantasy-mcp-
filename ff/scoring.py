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
    "IDP_FLEX": (),
}

# How a generic FLEX slot tends to get filled in practice. Used only to place
# replacement level, so it needs to be roughly right, not exact.
FLEX_SPLIT = {"RB": 0.45, "WR": 0.45, "TE": 0.10}
SUPERFLEX_SPLIT = {"QB": 0.7, "RB": 0.1, "WR": 0.15, "TE": 0.05}

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


def starter_counts(roster_positions: list[str], num_teams: int) -> dict[str, float]:
    """League-wide count of startable slots per real position.

    Flex slots are spread across their eligible positions using FLEX_SPLIT, so
    that a 2-flex league correctly pushes RB/WR replacement level deeper than a
    0-flex league would.
    """
    counts: dict[str, float] = {"QB": 0.0, "RB": 0.0, "WR": 0.0, "TE": 0.0}

    for slot in roster_positions:
        if slot in NON_PLAYER_SLOTS or slot == "DEF" or slot == "K":
            continue
        if slot in counts:
            counts[slot] += 1
            continue
        if slot == "SUPER_FLEX":
            for pos, share in SUPERFLEX_SPLIT.items():
                counts[pos] += share
        elif slot in FLEX_ELIGIBLE:
            eligible = FLEX_ELIGIBLE[slot]
            shares = {p: FLEX_SPLIT.get(p, 0.0) for p in eligible}
            total = sum(shares.values()) or 1.0
            for pos, share in shares.items():
                counts[pos] += share / total

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
        if pos in baselines:
            idx = max(0, int(round(baselines[pos])) - 1)
        else:
            # DEF/K: one per team, so the last starter is roughly num_teams deep.
            idx = num_teams - 1
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
