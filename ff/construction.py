"""Roster construction risk - correlation, concentration, and stashing.

Three things a per-player value model cannot see, because they are properties
of the roster rather than of any player in it:

  1. **Correlation.** A QB and his own top receiver rise and fall together. In
     best ball and DFS that is a feature - you want the weeks where both go off.
     In weekly head-to-head it is mostly a liability: it widens your own
     variance without raising your expected score, so a bad game for that
     offense becomes a bad game for two of your starters at once.

  2. **Concentration.** Several players from one NFL team is the same bet made
     repeatedly. A bye week, a change at offensive coordinator or a lopsided
     game script hits all of them together.

  3. **Stashing.** With IR slots you can roster a player who cannot help you
     now in exchange for what he becomes later. The slot is only free if it is
     genuinely a *reserve* slot - burning an active bench spot on someone who
     will not play for a month is a real cost.

None of these should override a large value gap. They are tiebreakers, and
they are reported as such.
"""

from __future__ import annotations

# Positions whose production is tied to the same passing game.
PASS_CATCHERS = {"WR", "TE"}


def find_stacks(players: list[dict]) -> list[dict]:
    """QB + pass-catcher pairs from the same NFL team."""
    qbs = [p for p in players if p.get("position") == "QB" and p.get("team")]
    stacks: list[dict] = []

    for qb in qbs:
        mates = [
            p
            for p in players
            if p.get("position") in PASS_CATCHERS
            and p.get("team") == qb.get("team")
            and p.get("player_id") != qb.get("player_id")
        ]
        if mates:
            stacks.append(
                {
                    "quarterback": qb["name"],
                    "team": qb.get("team"),
                    "pass_catchers": [
                        {"name": m["name"], "position": m["position"]} for m in mates
                    ],
                    "size": 1 + len(mates),
                }
            )
    return stacks


def team_concentration(players: list[dict], threshold: int = 3) -> list[dict]:
    """NFL teams you are over-exposed to."""
    by_team: dict[str, list[dict]] = {}
    for p in players:
        if p.get("team"):
            by_team.setdefault(p["team"], []).append(p)

    return [
        {
            "team": team,
            "count": len(group),
            "players": [f"{p['name']} ({p['position']})" for p in group],
            "shared_bye": group[0].get("bye"),
        }
        for team, group in sorted(by_team.items(), key=lambda x: -len(x[1]))
        if len(group) >= threshold
    ]


def correlation_report(players: list[dict], is_best_ball: bool = False) -> dict:
    """Correlation and concentration risk across a roster."""
    stacks = find_stacks(players)
    concentration = team_concentration(players)

    notes: list[str] = []
    for s in stacks:
        names = ", ".join(m["name"] for m in s["pass_catchers"])
        if is_best_ball:
            notes.append(
                f"{s['quarterback']} + {names} ({s['team']}) - a stack, which is "
                "an advantage in best ball: their big weeks land together"
            )
        else:
            notes.append(
                f"{s['quarterback']} + {names} ({s['team']}) - correlated starters. "
                "In weekly head-to-head this widens your variance without raising "
                "your expected score"
            )

    for c in concentration:
        bye = f", all off in week {c['shared_bye']}" if c.get("shared_bye") else ""
        notes.append(
            f"{c['count']} players from {c['team']}{bye} - concentrated exposure "
            "to one offense"
        )

    return {
        "stacks": stacks,
        "team_concentration": concentration,
        "notes": notes or ["no meaningful correlation or concentration risk"],
        "guidance": (
            "Correlation is a tiebreaker, not a veto - never pass on a clearly "
            "better player to avoid it."
        ),
    }


def stash_candidates(
    available: list[dict],
    reserve_slots: int,
    limit: int = 10,
) -> dict:
    """Injured or sidelined players worth holding on IR.

    The trade is simple: a reserve slot costs you nothing in the active roster,
    so anyone with real upside once healthy is worth the space. It only becomes
    a mistake when the league has no reserve slots and the cost is a live bench
    spot instead.
    """
    if not reserve_slots:
        return {
            "reserve_slots": 0,
            "candidates": [],
            "note": (
                "This league has no IR slots, so a stash costs an active bench "
                "spot. Only worth it for genuinely high-upside players."
            ),
        }

    hurt = [
        p
        for p in available
        if p.get("injury_status")
        and str(p["injury_status"]).upper() not in ("QUESTIONABLE", "PROBABLE")
    ]
    hurt.sort(key=lambda p: p.get("vor") or 0, reverse=True)

    return {
        "reserve_slots": reserve_slots,
        "candidates": [
            {
                "name": p["name"],
                "position": p["position"],
                "team": p.get("team"),
                "injury_status": p.get("injury_status"),
                "projected_points": p.get("points"),
                "vor": p.get("vor"),
                "adp": p.get("adp"),
            }
            for p in hurt[:limit]
        ],
        "note": (
            f"{reserve_slots} IR slots available. A player on IR does not occupy "
            "an active roster spot, so stashing real talent is close to free."
        ),
    }


def waiver_priority_advice(
    position_in_order: int | None, total_teams: int, week: int | None
) -> dict:
    """Strategy for leagues using waiver priority rather than FAAB.

    Priority is a single-use asset with no partial spend: unlike FAAB you
    cannot commit half of it. That makes the decision binary and makes timing
    the whole game - using it on a marginal add early means not having it when
    a genuine league-winner appears.
    """
    if position_in_order is None:
        return {
            "note": "Waiver priority order unavailable from the league settings."
        }

    top_third = total_teams / 3
    advice: list[str] = []

    if position_in_order <= top_third:
        advice.append(
            f"You hold priority {position_in_order} of {total_teams} - a genuinely "
            "valuable asset. Spend it only on a player who changes your season."
        )
    elif position_in_order >= total_teams - top_third:
        advice.append(
            f"You are near the back of the order ({position_in_order} of "
            f"{total_teams}). Claim freely - you lose little by using it."
        )
    else:
        advice.append(f"Mid-order priority ({position_in_order} of {total_teams}).")

    if week is not None and week <= 4:
        advice.append(
            "Early season: the best waiver adds of the year usually surface in "
            "weeks 2-5, so holding priority for a marginal upgrade now is "
            "usually right."
        )
    elif week is not None and week >= 11:
        advice.append(
            "Late season: priority has little remaining value, so there is no "
            "reason to hoard it."
        )

    return {
        "priority_position": position_in_order,
        "total_teams": total_teams,
        "advice": advice,
        "note": (
            "Priority is all-or-nothing - you cannot bid half of it - so timing "
            "matters more than it does with FAAB."
        ),
    }
