"""Who will actually be there when you pick.

The draft board tells you who is best. It does not tell you who you can
realistically have, and at a late slot those are very different lists. Telling
someone picking 7th to take the consensus 1.01 is worse than useless - it burns
the pick they were supposed to be planning.

So we model where each player actually goes. ADP is a mean, not a certainty:
a player with an ADP of 20 goes at 14 in some drafts and 28 in others. Treating
his draft position as a normal distribution around his ADP gives a probability
that he survives to a given pick.

Spread grows with ADP, because the market agrees far more about the first pick
than about the fiftieth. An early-round player's range is a couple of picks; a
tenth-rounder's is most of a round either way.
"""

from __future__ import annotations

import math

# Below this probability a player is a long shot; above it he is near-certain
# and not really a "decision" at that pick.
LONGSHOT = 0.15
NEAR_CERTAIN = 0.90


def _spread(adp: float) -> float:
    """Standard deviation of actual draft position around ADP."""
    # Floor of 3 picks keeps the very top of the board from being treated as
    # deterministic; 22% growth reflects widening disagreement deeper in.
    return max(3.0, 0.22 * adp)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def availability_probability(adp: float | None, pick: int) -> float | None:
    """P(player is still on the board when `pick` comes round)."""
    if adp is None:
        # No ADP usually means deep sleeper - effectively always available.
        return 0.97
    sd = _spread(adp)
    # He survives if his realised draft slot lands at or beyond this pick.
    return 1.0 - _normal_cdf((pick - adp) / sd)


def picks_for_slot(
    slot: int,
    teams: int,
    rounds: int,
    draft_type: str = "snake",
    reversal_round: int = 0,
) -> list[int]:
    """Overall pick numbers for a draft slot.

    Sleeper supports more than one draft format, and assuming snake silently
    hands a linear-draft manager the wrong picks for every round after the
    first - which is worse than refusing to answer.

    - **snake**: order reverses each round.
    - **linear**: the same order every round, so a late slot is late all draft.
    - **reversal_round** (third-round reversal and friends): snake until that
      round, then the order flips again and snakes on from there, so the
      round that reverses repeats the previous round's order.
    - **auction**: there are no pick slots at all; the caller must handle it.
    """
    if draft_type == "auction":
        return []

    picks = []
    for rd in range(1, rounds + 1):
        if draft_type == "linear":
            reversed_round = False
        else:
            reversed_round = rd % 2 == 0
            # After the reversal round the parity flips, so the reversal round
            # itself keeps the previous round's order.
            if reversal_round and rd >= reversal_round:
                reversed_round = not reversed_round

        if reversed_round:
            picks.append((rd - 1) * teams + (teams - slot + 1))
        else:
            picks.append((rd - 1) * teams + slot)
    return picks


def targets_at_pick(
    rows: list[dict],
    pick: int,
    taken: set[str] | None = None,
    limit: int = 8,
) -> list[dict]:
    """Realistic targets at one pick, ranked by value weighted by availability."""
    taken = taken or set()
    out = []
    for r in rows:
        if r["player_id"] in taken:
            continue
        p = availability_probability(r.get("adp"), pick)
        if p is None or p < LONGSHOT:
            continue
        vor = r.get("vor") or 0
        out.append(
            {
                "name": r["name"],
                "position": r["position"],
                "team": r.get("team"),
                "points": r["points"],
                "vor": vor,
                "adp": r.get("adp"),
                "available_pct": round(100 * p),
                # Rank by what you can actually expect to get, not by raw value.
                "expected_value": round(vor * p, 1),
                "status": (
                    "near-certain" if p >= NEAR_CERTAIN
                    else "coin-flip" if p >= 0.45
                    else "long shot"
                ),
            }
        )
    out.sort(key=lambda x: x["expected_value"], reverse=True)
    return out[:limit]


def fallers_at_pick(
    rows: list[dict],
    pick: int,
    baseline_vor: float,
    taken: set[str] | None = None,
    limit: int = 4,
) -> list[dict]:
    """High-value players with a real but unlikely chance of reaching you.

    Ranking purely by expected value buries these: a 21% shot at the best
    player on the board scores worse than a near-certain lesser one, so he
    drops off the list entirely. But that is precisely the pick a manager needs
    warning about in advance - if he falls, you take him without thinking.
    """
    taken = taken or set()
    out = []
    for r in rows:
        if r["player_id"] in taken:
            continue
        p = availability_probability(r.get("adp"), pick)
        vor = r.get("vor") or 0
        # Only interesting if he beats what you could otherwise near-certainly
        # get, and is genuinely unlikely to be there.
        if p is None or not (0.08 <= p < 0.55) or vor <= baseline_vor:
            continue
        out.append(
            {
                "name": r["name"],
                "position": r["position"],
                "vor": vor,
                "adp": r.get("adp"),
                "available_pct": round(100 * p),
                "upgrade_over_baseline": round(vor - baseline_vor, 1),
            }
        )
    out.sort(key=lambda x: x["vor"], reverse=True)
    return out[:limit]


def plan(
    rows: list[dict],
    slot: int,
    teams: int,
    rounds: int,
    through_round: int = 6,
    limit: int = 6,
    draft_type: str = "snake",
    reversal_round: int = 0,
) -> list[dict]:
    """A round-by-round view of who should be there at each of your picks."""
    picks = picks_for_slot(slot, teams, rounds, draft_type, reversal_round)[
        :through_round
    ]
    out = []
    for i, pick in enumerate(picks, start=1):
        targets = targets_at_pick(rows, pick, limit=limit)
        gap = (picks[i] - pick) if i < len(picks) else None
        # Baseline = the best you can bank on at this pick.
        baseline = max(
            (t["vor"] for t in targets if t["available_pct"] >= 85), default=0.0
        )
        out.append(
            {
                "round": i,
                "overall_pick": pick,
                "picks_until_next": gap,
                "targets": targets,
                "if_they_fall": fallers_at_pick(rows, pick, baseline),
            }
        )
    return out


def plan_notes(picks: list[int], teams: int) -> list[str]:
    """Structural observations about a slot, before any player is named."""
    notes: list[str] = []
    if len(picks) < 2:
        return notes

    first_gap = picks[1] - picks[0]
    notes.append(
        f"Your first two picks are {picks[0]} and {picks[1]} - only {first_gap} "
        "picks apart."
    )
    if first_gap <= 4:
        notes.append(
            "That is effectively a double-header: plan the pair together, since "
            "you can take one of two positions knowing the other is still likely "
            "to be there."
        )
    elif first_gap >= 15:
        notes.append(
            "That is a long wait. Favour the scarcer position first, because the "
            "board will move a long way before you pick again."
        )

    notes.append(
        f"Turn picks come in pairs all draft; by round {len(picks)} you are at "
        f"pick {picks[-1]}."
    )
    return notes
