"""Keeper decisions - is a player worth the pick he costs?

A keeper is never free. Keeping someone forfeits a draft pick, so the real
question is not "is this player good?" but "is he better than whoever I would
have taken with the pick he costs?" That difference is his **surplus**, and it
is the only number that should drive the decision.

Two consequences fall straight out of that framing:

  - An elite player at an expensive cost can be a bad keeper. Keeping the
    consensus 1.02 at a first-round price buys you nothing you could not have
    had by simply drafting him.
  - A modest player at a cheap cost can be an excellent keeper. A useful
    starter kept in the fourteenth round is most of a free roster spot.

The cost side comes from the league's own rule, which Sleeper does not encode -
it stores *that* keepers exist (`max_keepers`, `roster.keepers`, and an
`is_keeper` flag on draft picks) but not the price. So the rule is a parameter,
defaulting to the most common convention: a keeper costs the round in which he
was drafted the previous season.

The value side comes from ADP: the pick a keeper costs would otherwise have
returned roughly the player the market drafts at that slot, so that player's
VOR is the baseline the keeper has to beat.
"""

from __future__ import annotations

import statistics

from . import sleeper

# How the league prices a keeper. Sleeper does not store this, so it is stated
# rather than inferred.
COST_RULES = {
    "same_round": 0,  # costs the round he was drafted last year
    "round_earlier": -1,  # a round earlier - the most common escalator
    "two_rounds_earlier": -2,
}


def draft_length(lg: dict) -> int | None:
    """How many rounds the draft actually runs.

    Read from the draft object, not from the league's `settings.draft_rounds`
    - that field is present but means something else (a league whose draft is
    15 rounds reports 3), and trusting it silently clamps every keeper cost.
    """
    draft_id = lg.get("draft_id")
    if not draft_id:
        return None
    try:
        return ((sleeper.draft(draft_id).get("settings") or {}).get("rounds")) or None
    except Exception:
        return None


def keeper_settings(lg: dict) -> dict:
    """What the league's own settings say about keepers."""
    settings = lg.get("settings") or {}
    max_keepers = settings.get("max_keepers") or 0
    return {
        "max_keepers": max_keepers,
        # A league can carry a non-zero max_keepers it never actually uses, so
        # "configured" and "in use" are reported separately rather than
        # collapsed into one flag.
        "configured": max_keepers > 0,
        "teams": lg.get("total_rosters"),
    }


def keepers_in_use(league_id: str) -> bool:
    """Whether any roster has actually designated a keeper."""
    try:
        return any(r.get("keepers") for r in sleeper.league_rosters(league_id))
    except Exception:
        return False


def prior_draft_rounds(lg: dict) -> tuple[dict[str, int], str | None]:
    """{player_id: round drafted} from last season, plus a note if unavailable.

    This is what prices a keeper under the standard rule. It needs the league to
    have a previous season linked; a first-year league has no prior draft and
    therefore no derivable cost.
    """
    prev = lg.get("previous_league_id")
    if not prev:
        return {}, (
            "No previous season is linked to this league, so last year's draft "
            "rounds are unavailable. Supply costs manually to price keepers."
        )

    try:
        prev_lg = sleeper.league(prev)
        draft_id = prev_lg.get("draft_id")
        picks = sleeper.draft_picks(draft_id) if draft_id else []
    except Exception as e:
        return {}, f"Could not read last season's draft: {type(e).__name__}"

    if not picks:
        return {}, "Last season's draft has no picks recorded."

    return {
        p["player_id"]: p.get("round")
        for p in picks
        if p.get("player_id") and p.get("round")
    }, None


def _pick_number(round_no: int, slot: int, teams: int) -> int:
    """Overall pick number for a round/slot in a snake draft."""
    if round_no % 2 == 1:
        return (round_no - 1) * teams + slot
    return (round_no - 1) * teams + (teams - slot + 1)


def baseline_at_pick(rows: list[dict], pick: int, window: int = 3) -> float:
    """What a pick is worth: the VOR the market returns around that slot.

    Uses ADP rather than a raw VOR ranking, because the pick doesn't return the
    Nth-best player - it returns whoever is actually available there.
    """
    near = [
        r["vor"]
        for r in rows
        if r.get("adp") and abs(r["adp"] - pick) <= window and r.get("vor") is not None
    ]
    if near:
        # Floored at zero: a pick cannot be worth *less* than nothing. Deep
        # picks return players below replacement, and letting that go negative
        # made a replacement-level keeper look like a bargain purely because
        # the pick it cost scored -44.
        return max(0.0, statistics.median(near))

    # Past the end of ADP coverage, the best still on the board - not the worst.
    later = [r["vor"] for r in rows if r.get("adp") and r["adp"] > pick]
    return max(0.0, max(later)) if later else 0.0


def analyse(
    owned: list[dict],
    rows: list[dict],
    lg: dict,
    slot: int | None = None,
    cost_rule: str = "same_round",
    prior_rounds: dict[str, int] | None = None,
    undrafted_cost_round: int | None = None,
    draft_rounds: int | None = None,
) -> list[dict]:
    """Rank a roster's keeper candidates by surplus value.

    `undrafted_cost_round` prices a player who went undrafted last season -
    leagues usually assign these a last-round or free cost. Left unset, such
    players are reported without a surplus rather than guessed at.
    """
    # Both come from the league the caller configured. No default team count
    # or draft length is assumed - a guess here silently prices every keeper
    # against the wrong board.
    teams = lg.get("total_rosters")
    rounds = draft_rounds
    if not teams or not rounds:
        return [
            {
                "player": p["name"],
                "position": p["position"],
                "surplus": None,
                "verdict": (
                    "cannot price - league team count or draft length "
                    "unavailable from the API"
                ),
            }
            for p in owned
        ]
    shift = COST_RULES.get(cost_rule, 0)
    prior_rounds = prior_rounds or {}

    out: list[dict] = []
    for p in owned:
        drafted = prior_rounds.get(p["player_id"])
        cost_round = None

        if drafted:
            cost_round = max(1, drafted + shift)
        elif undrafted_cost_round:
            cost_round = undrafted_cost_round

        entry = {
            "player": p["name"],
            "position": p["position"],
            "team": p.get("team"),
            "projected_points": p.get("points"),
            "vor": p.get("vor"),
            "adp": p.get("adp"),
            "drafted_round_last_season": drafted,
            "keeper_cost_round": cost_round,
        }

        if cost_round is None:
            entry["surplus"] = None
            entry["verdict"] = "cost unknown - supply a keeper cost to evaluate"
            out.append(entry)
            continue

        cost_round = min(cost_round, rounds)
        pick = _pick_number(cost_round, slot or 1, teams)
        baseline = baseline_at_pick(rows, pick)
        surplus = (p.get("vor") or 0) - baseline

        entry.update(
            {
                "keeper_cost_pick": pick,
                "value_of_that_pick": round(baseline, 1),
                "surplus": round(surplus, 1),
                "verdict": _verdict(surplus),
            }
        )
        out.append(entry)

    out.sort(key=lambda x: (x["surplus"] is None, -(x["surplus"] or 0)))
    return out


def _verdict(surplus: float) -> str:
    if surplus >= 60:
        return "outstanding keeper - far more than the pick returns"
    if surplus >= 25:
        return "clear keeper"
    if surplus >= 5:
        return "marginal keeper - worth it, but not by much"
    if surplus >= -15:
        return "roughly break-even - the pick buys you the same thing"
    return "do not keep - the pick is worth more than the player"


def recommend(analysed: list[dict], max_keepers: int) -> dict:
    """Which of the candidates to actually keep."""
    scored = [a for a in analysed if a.get("surplus") is not None]
    positive = [a for a in scored if a["surplus"] > 0]
    keep = positive[:max_keepers]

    if not scored:
        advice = "No keeper costs are known, so nothing can be priced yet."
    elif not positive:
        advice = (
            "Keep nobody. Every candidate costs a pick worth at least as much as "
            "the player - you are better off entering the draft with all picks."
        )
    elif len(keep) < max_keepers:
        advice = (
            f"Keep {len(keep)} of your {max_keepers} slots. The rest of the roster "
            "is worth less than the picks it would cost, and an unused keeper slot "
            "is not a wasted one."
        )
    else:
        advice = f"Keep {', '.join(k['player'] for k in keep)}."

    return {
        "keep": keep,
        "advice": advice,
        "note": (
            "Surplus = the player's VOR minus the value of the pick he costs. "
            "A great player at an expensive cost can still be a poor keeper."
        ),
    }
