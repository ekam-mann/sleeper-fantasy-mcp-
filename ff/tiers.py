"""Tier detection — where the talent cliffs actually fall.

A ranked list invites you to treat #7 and #8 as near-equals. Often they are,
and sometimes #8 is the start of a much worse group. Tiers make the cliff
visible so you know when you can wait a round and when you cannot.

Method: sort a position by value, walk down it, and start a new tier wherever
the drop to the next player is unusually large relative to the typical drop
within that position. No fixed thresholds — the gap that matters at QB is a
different size from the one that matters at TE.
"""

from __future__ import annotations

import statistics


def assign_tiers(
    rows: list[dict],
    position: str,
    value_key: str = "vor",
    max_players: int = 40,
    sensitivity: float = 1.4,
) -> list[dict]:
    """Group one position into tiers. Returns [{tier, players:[...]}, ...]."""
    pool = [r for r in rows if r.get("position") == position and r.get(value_key) is not None]
    pool.sort(key=lambda r: r[value_key], reverse=True)
    pool = pool[:max_players]
    if len(pool) < 3:
        return [{"tier": 1, "players": pool}] if pool else []

    drops = [
        pool[i][value_key] - pool[i + 1][value_key] for i in range(len(pool) - 1)
    ]
    # A "cliff" is a drop meaningfully larger than this position's normal step.
    typical = statistics.median(drops)
    spread = statistics.pstdev(drops) or 1.0
    threshold = typical + sensitivity * spread

    tiers: list[dict] = []
    current = [pool[0]]
    for i, drop in enumerate(drops):
        if drop >= threshold and len(current) >= 1:
            tiers.append({"tier": len(tiers) + 1, "players": current})
            current = []
        current.append(pool[i + 1])
    if current:
        tiers.append({"tier": len(tiers) + 1, "players": current})

    for t in tiers:
        vals = [p[value_key] for p in t["players"]]
        t["value_range"] = [round(min(vals), 1), round(max(vals), 1)]
        t["count"] = len(t["players"])
    return tiers


def tier_break_warning(rows: list[dict], position: str, available: set[str]) -> str | None:
    """Flag when only a couple of players remain in the current top tier.

    This is the actionable form of tier logic during a live draft: 'two left in
    this tier, then it falls off a cliff' is what changes a pick.
    """
    tiers = assign_tiers(rows, position)
    for t in tiers:
        left = [p for p in t["players"] if p["player_id"] in available]
        if not left:
            continue
        if len(left) <= 2:
            names = ", ".join(p["name"] for p in left)
            return (
                f"{position}: only {len(left)} left in tier {t['tier']} ({names}) "
                f"before a drop-off — last chance at this value band"
            )
        return None
    return None
