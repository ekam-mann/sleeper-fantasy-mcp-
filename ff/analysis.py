"""Decision helpers: roster needs, draft value, trades, waivers, lineups."""

from __future__ import annotations

from . import scoring


def roster_needs(
    owned: list[dict],
    roster_positions: list[str],
    num_teams: int,
) -> dict[str, dict]:
    """How urgently each position still needs bodies.

    Compares what you own against what the starting lineup demands, then
    weights by scarcity so that "need a TE" and "need a 4th WR" don't read as
    equally urgent.
    """
    required = scoring.starter_counts(roster_positions, num_teams)
    per_team = {pos: n / num_teams for pos, n in required.items()}

    have: dict[str, int] = {}
    for p in owned:
        have[p["position"]] = have.get(p["position"], 0) + 1

    out: dict[str, dict] = {}
    for pos, need in per_team.items():
        n = have.get(pos, 0)
        shortfall = max(0.0, need - n)
        if shortfall > 0:
            urgency = "critical"
        elif n <= need + 0.5:
            urgency = "thin"
        elif n <= need + 2:
            urgency = "adequate"
        else:
            urgency = "surplus"
        out[pos] = {
            "have": n,
            "starters_needed": round(need, 1),
            "shortfall": round(shortfall, 1),
            "urgency": urgency,
        }
    return out


# What a player who cannot crack your starting lineup is still worth: the weeks
# the man ahead of him misses, plus his bye. That is not one number - it is
# strongly positional, and a flat constant gets defences badly wrong.
#
# Derived from this repo's own availability table (2023-2025, players with a
# 100+ point season, so the sample is starters rather than deep reserves):
#
#     pos   games missed/season   (missed + 1 bye) / 17
#     QB           5.53                   0.38
#     RB           2.86                   0.23
#     WR           3.00                   0.24
#     TE           2.48                   0.20
#
# QB is highest because the number counts benchings, not just injuries - and a
# benched starter puts the backup in your lineup exactly as an injured one does,
# so it belongs here.
#
# Refresh with: availability.position_base_rates(availability.availability_table(...))
BENCH_SHARE_BY_POS = {
    "QB": 0.38,
    "RB": 0.23,
    "WR": 0.24,
    "TE": 0.20,
    # A team defence and a kicker never miss a game - there is always one on the
    # field - so the only week you need a second is the bye, and even that is
    # streamable off waivers. Measured durability does not apply; the honest
    # value of a backup here is the bye week and nothing more.
    "DEF": 1 / 17,
    "K": 1 / 17,
}

# Positions with no measured durability (IDP, or anything Sleeper adds later).
# Set to the all-position average so an unknown position behaves sensibly
# rather than being silently valued at zero.
BENCH_SHARE_DEFAULT = 0.24


def bench_share(position: str | None) -> float:
    """Fraction of a season a backup at this position can expect to start."""
    return BENCH_SHARE_BY_POS.get(position or "", BENCH_SHARE_DEFAULT)


def starting_value(players: list[dict], roster_positions: list[str]) -> float:
    """Projected points of the best legal starting lineup from these players."""
    return optimal_lineup(players, roster_positions)["projected_total"]


def marginal_value(
    candidate: dict, owned: list[dict], roster_positions: list[str]
) -> float:
    """How much this player improves your lineup *over a replacement at his slot*.

    VOR asks "how much better is he than a replacement at his position?" That is
    the right question for a draft board and the wrong one for a roster, because
    it never notices you can only start so many of a position. In a one-TE
    league a second tight end carries a large VOR - he really is far better than
    the last startable TE league-wide - while improving your lineup by almost
    nothing, because your TE slot is filled and tight ends rarely win a flex
    spot. That is how a board recommends TE4 over WR5.

    Recomputing the optimal lineup with and without him answers that directly,
    with no positional special-casing: the roster shape decides what can be
    started, so a league where TE *is* flex-eligible gets a different answer
    for free.

    The subtlety is what to compare against. Comparing to the lineup *without*
    him measures raw points, not value, and raw points are on a different scale
    from VOR - so blending the two ranks them nonsensically. An empty DEF slot
    is the clearest case: the best defence adds his whole ~116-point season to
    an empty slot, which floats every defence and kicker to the top of the
    board. But the *choice* is not worth 116 points, because the waiver-wire
    defence you would otherwise start is worth ~97 of them. The decision is
    worth the difference.

    So the baseline is the lineup with a replacement-level player at the
    candidate's position. That puts the result on the same scale as VOR by
    construction: for an open slot it reduces to roughly his VOR, and for a
    slot he cannot crack it reduces to zero.

    Replacement level needs no extra plumbing - it is already implied by the
    row, since vor = points - replacement.
    """
    vor = candidate.get("vor")
    points = candidate.get("points")

    if vor is None or points is None:
        # No VOR to anchor on; fall back to the raw improvement.
        baseline = owned
    else:
        replacement = {
            **candidate,
            "player_id": "__replacement__",
            "name": "replacement",
            "points": points - vor,
        }
        baseline = [*owned, replacement]

    return starting_value([*owned, candidate], roster_positions) - starting_value(
        baseline, roster_positions
    )


# Positions where a startable player is always available on waivers, so you
# stream them week to week instead of rostering depth.
#
# These need a hard cap, not just a small bench share. Only about as many
# defences are drafted as there are teams, while roughly three times as many
# receivers clear replacement - so once a draft passes its twelfth round every
# skill position has gone below replacement and DEF is the *only* position with
# positive VOR left on the board. Any bench value above zero then wins by
# default, and a full mock draft from 1.1 took four defences.
#
# Value over replacement is measured against the last *startable* player league
# wide, which is the right denominator for a starter and the wrong one for a
# backup you would never play: the true alternative to a second defence is not
# the 13th-best defence, it is whichever one is free on waivers that week -
# which is roughly as good. So the surplus is not small, it is zero.
STREAMABLE_POSITIONS = ("DEF", "K")


def roster_cap(position: str | None, roster_positions: list[str]) -> float | None:
    """Most of this position worth rostering, or None if uncapped."""
    if position not in STREAMABLE_POSITIONS:
        return None
    return scoring.starter_counts(roster_positions, 1).get(position, 0.0)


def _depth_discount(
    candidate: dict, owned: list[dict], roster_positions: list[str]
) -> float:
    """How much a bench player's depth value decays behind those ahead of him.

    Bench value is not proportional to VOR - it is proportional to how likely
    you are to need him. The first backup at a position is genuinely useful:
    starters miss time. The fourth is not, because three players have to fall
    over before he plays.

    Without this, a roster full of tight ends keeps rating the next tight end
    above a receiver, since a flat share of VOR preserves the very ordering the
    lineup check was meant to correct.
    """
    return 1.0 / (1.0 + _position_surplus(candidate, owned, roster_positions))


def _position_surplus(
    candidate: dict, owned: list[dict], roster_positions: list[str]
) -> float:
    """How many bodies deep past startable this position already is."""
    pos = candidate.get("position")
    ahead = sum(1 for o in owned if o.get("position") == pos)
    # Startable slots for this position on a single roster, flex share included.
    per_team = scoring.starter_counts(roster_positions, 1).get(pos, 0.0)
    return max(0.0, ahead - per_team)


def effective_value(
    candidate: dict, owned: list[dict], roster_positions: list[str]
) -> dict:
    """Lineup improvement, floored by what the player is worth as depth.

    Ranking on lineup improvement alone would price every backup at exactly
    zero, which overcorrects - depth wins seasons. So the score is the better
    of what he adds to the lineup now and what he is worth behind the players
    already there, which decays the deeper he sits.
    """
    vor = candidate.get("vor") or 0.0
    marginal = marginal_value(candidate, owned, roster_positions)
    # max(0, vor) matters: a below-replacement player must not earn a *floor*
    # from a negative number.
    surplus = _position_surplus(candidate, owned, roster_positions)
    discount = 1.0 / (1.0 + surplus)
    bench_floor = bench_share(candidate.get("position")) * max(0.0, vor) * discount

    if vor > 0:
        score = max(marginal, bench_floor)
    else:
        # Below replacement the bench floor is zero for everyone, so every such
        # player ties at exactly 0.0 and the ranking among them is decided by
        # list order - which is no ranking at all. In the last rounds, when the
        # whole board is below replacement, that tie is the entire decision, and
        # it fell to tight ends: TE replacement level is shallow, so a late TE
        # scores a mildly negative VOR where an equally unplayable receiver
        # scores a deeply negative one.
        #
        # Ranking them by VOR does not work either. How far below replacement a
        # player sits is not decision-relevant once he is below it at all - he
        # will not start either way - but the magnitudes differ wildly by
        # position, because replacement level sits at a different depth for
        # each. A late TE grades around -5 where an equally unplayable WR
        # grades around -25, purely because only ~12 TEs clear replacement
        # against ~35 WRs. Sorting on that number is sorting on positional
        # scarcity of the *starting* pool, and it fills the last rounds with
        # tight ends.
        #
        # What is decision-relevant is where you are thinnest, so rank on
        # depth alone and let raw VOR break ties within a position.
        # NOT max(marginal, -surplus): marginal is 0.0 for every bench player,
        # so the max would return 0.0 every time and restore the very tie this
        # branch exists to break.
        score = marginal if marginal > 0 else -surplus

    pos = candidate.get("position")
    ahead = sum(1 for o in owned if o.get("position") == pos)

    if not owned:
        why = "no roster yet - full value"
    elif marginal >= bench_floor and marginal > 0:
        why = f"improves your starting lineup by {marginal:.1f}"
    elif vor > 0 and discount < 1.0:
        why = (
            f"cannot crack your lineup and you already roster {ahead} at {pos} - "
            f"depth value discounted to {discount:.0%}"
        )
    elif vor > 0:
        why = (
            f"cannot crack your lineup (adds {marginal:.1f}) - valued as depth "
            f"at {bench_share(pos):.0%} of his VOR"
        )
    else:
        why = "below replacement at his position"

    return {
        "effective_value": round(score, 1),
        "marginal_to_lineup": round(marginal, 1),
        "bench_floor": round(bench_floor, 1),
        "depth_discount": round(discount, 2),
        "vor": round(vor, 1),
        "why": why,
    }


def _need_multiplier(urgency: str) -> float:
    return {
        "critical": 1.15,
        "thin": 1.05,
        "adequate": 1.0,
        "surplus": 0.88,
    }.get(urgency, 1.0)


def apply_need(vor: float, urgency: str) -> float:
    """Tilt a value by positional need, without inverting below zero.

    Scaling a signed number by a multiplier does the opposite of what it means
    once the number goes negative: a 0.88 "penalty" on a VOR of -60 returns
    -52.8, which *promotes* the player. The old code did exactly that, so a
    surplus position was penalised above replacement and rewarded below it,
    while a critical need was rewarded above and punished below. Both backwards.

    The tilt belongs on the upside only. A below-replacement player is not made
    more attractive by the position being thin, nor less by it being crowded -
    he is below replacement either way.
    """
    if vor <= 0:
        return vor
    return vor * _need_multiplier(urgency)


def draft_recommendations(
    available: list[dict],
    owned: list[dict],
    roster_positions: list[str],
    num_teams: int,
    pick_number: int | None,
    limit: int = 12,
) -> list[dict]:
    """Rank the best available players for *this* roster at *this* pick.

    Blends three things:
      - VOR (value over replacement, in your league's scoring)
      - positional need on your current roster
      - ADP value (are you reaching, or is he falling to you?)
    """
    needs = roster_needs(owned, roster_positions, num_teams)

    held: dict[str, int] = {}
    for o in owned:
        held[o["position"]] = held.get(o["position"], 0) + 1

    # Which positions this league can start at all, flex eligibility included.
    startable = scoring.starter_counts(roster_positions, 1)

    scored: list[dict] = []
    for p in available:
        # A position with no slot and no flex that accepts it can never enter a
        # lineup, so it is not a worse pick - it is not a pick. Sleeper's player
        # pool carries positions most leagues never start (FB is the common one,
        # and kickers in a league with no K slot), and they surface at exactly
        # the wrong moment: they grade at a VOR of 0 rather than negative,
        # because replacement level for an unstartable position is its best
        # player. In the last rounds, when every startable player has gone below
        # replacement, that 0 is the highest number left and a mock draft from
        # 1.1 duly took two fullbacks.
        if startable.get(p["position"], 0.0) <= 0:
            continue

        # Never recommend depth at a position you would stream instead.
        cap = roster_cap(p["position"], roster_positions)
        if cap is not None and held.get(p["position"], 0) >= cap:
            continue

        urgency = needs.get(p["position"], {}).get("urgency", "adequate")
        # Rank on what he adds to *this* roster, not on raw VOR. The need tilt
        # then breaks ties among players who help a similar amount.
        fit = effective_value(p, owned, roster_positions)
        adjusted = apply_need(fit["effective_value"], urgency)

        adp = p.get("adp")
        if adp and pick_number:
            # Positive = he has fallen past his ADP and is a bargain here.
            adp_delta = adp - pick_number
        else:
            adp_delta = None

        reasons = []
        need_info = needs.get(p["position"], {})
        if owned and fit["marginal_to_lineup"] <= 0 and (p.get("vor") or 0) > 0:
            reasons.append(
                f"adds nothing to your lineup — you already start "
                f"{need_info.get('have', 0)} at {p['position']}"
            )
        elif need_info.get("urgency") == "critical":
            reasons.append(f"you have no starting {p['position']} yet")
        elif need_info.get("urgency") == "thin":
            reasons.append(f"{p['position']} is still thin ({need_info.get('have')} rostered)")
        elif need_info.get("urgency") == "surplus":
            reasons.append(f"you're already deep at {p['position']}")
        if adp_delta is not None and adp_delta >= 8:
            reasons.append(f"falling — ADP {adp:.0f}, you're on the clock at {pick_number}")
        elif adp_delta is not None and adp_delta <= -12:
            reasons.append(f"a reach — ADP {adp:.0f} vs pick {pick_number}")
        if p.get("injury_status"):
            reasons.append(f"injury flag: {p['injury_status']}")

        # Warn if this pick would stack a bye you're already loaded on.
        if p.get("bye"):
            same_bye = [o for o in owned if o.get("bye") == p["bye"]]
            if len(same_bye) >= 2:
                reasons.append(
                    f"bye week {p['bye']} — you'd have {len(same_bye) + 1} players out that week"
                )

        scored.append(
            {
                **p,
                "adjusted_value": round(adjusted, 1),
                "marginal_to_lineup": fit["marginal_to_lineup"],
                "bench_floor": fit["bench_floor"],
                "adp_delta": round(adp_delta, 1) if adp_delta is not None else None,
                "why": "; ".join(reasons) or "best value on the board",
            }
        )

    # VOR is the secondary key: below replacement every candidate is scored on
    # depth alone, so this decides between players at equally thin positions.
    scored.sort(key=lambda r: (r["adjusted_value"], r.get("vor") or 0.0), reverse=True)
    return scored[:limit]


def evaluate_trade(
    give: list[dict],
    get: list[dict],
    owned: list[dict],
    roster_positions: list[str],
    num_teams: int,
) -> dict:
    """Evaluate a proposed trade in raw value and in roster-fit terms."""
    give_val = sum(p["vor"] for p in give)
    get_val = sum(p["vor"] for p in get)
    delta = get_val - give_val

    # Roster fit: re-run needs as if the trade had happened.
    after = [p for p in owned if p["player_id"] not in {g["player_id"] for g in give}]
    after += get
    before_needs = roster_needs(owned, roster_positions, num_teams)
    after_needs = roster_needs(after, roster_positions, num_teams)

    fit_notes = []
    for pos in sorted(set(before_needs) | set(after_needs)):
        b = before_needs.get(pos, {}).get("urgency")
        a = after_needs.get(pos, {}).get("urgency")
        if b != a:
            fit_notes.append(f"{pos}: {b} -> {a}")

    # Consolidating two starters into one stud is worth something in a league
    # with a shallow bench, but costs you depth. Flag the shape of the trade.
    shape = None
    if len(get) < len(give):
        shape = "consolidation (fewer, better players — helps starting lineup, thins depth)"
    elif len(get) > len(give):
        shape = "fragmentation (more bodies, lower ceiling at the top)"

    if delta > 15:
        verdict = "clear win — take it"
    elif delta > 5:
        verdict = "modest win"
    elif delta >= -5:
        verdict = "roughly even — decide on roster fit, not value"
    elif delta >= -15:
        verdict = "modest loss"
    else:
        verdict = "clear loss — decline"

    return {
        "give": [{"name": p["name"], "pos": p["position"], "vor": p["vor"]} for p in give],
        "get": [{"name": p["name"], "pos": p["position"], "vor": p["vor"]} for p in get],
        "give_value": round(give_val, 1),
        "get_value": round(get_val, 1),
        "net_value": round(delta, 1),
        "verdict": verdict,
        "roster_fit_changes": fit_notes or ["no change to positional urgency"],
        "trade_shape": shape,
    }


def faab_bid(
    player: dict,
    budget_remaining: int,
    needs: dict,
    week: int | None = None,
    rival_budgets: list[int] | None = None,
) -> dict:
    """Suggest a FAAB bid, as a range, in the context of the season.

    Three things the raw value of a player does not tell you:

    1. **Time.** Budget is only worth what it buys. A player added in week 2
       plays fifteen more games for you; the same player in week 12 plays five.
       Unspent budget at the end of the year is wasted, so the same VOR should
       cost more early and less late.
    2. **Leverage.** What matters is not your budget but your budget relative to
       everyone else's. If rivals are broke you can win with the minimum; if
       they are flush you must pay up.
    3. **Precision.** A single number implies a confidence nobody has. A range
       lets you decide where in it to land.
    """
    vor = max(0.0, player.get("vor") or 0.0)
    urgency = needs.get(player["position"], {}).get("urgency", "adequate")

    # Anchor on value, then bend for need.
    if vor > 60:
        pct = 0.45
    elif vor > 35:
        pct = 0.25
    elif vor > 15:
        pct = 0.12
    elif vor > 0:
        pct = 0.05
    else:
        pct = 0.01

    pct *= _need_multiplier(urgency)

    # Time: weight by the share of the fantasy regular season still to play.
    timing_note = None
    if week:
        remaining = max(0, 15 - week)  # weeks 1-14 before most playoffs
        if remaining <= 2:
            pct *= 0.55
            timing_note = "late season - little runway left, so bid down"
        elif remaining <= 5:
            pct *= 0.8
            timing_note = "back half of the season - shorter payoff window"
        elif week <= 4:
            pct *= 1.25
            timing_note = "early season - a hit pays off for the whole year"

    # Leverage: your budget against the field's.
    leverage_note = None
    if rival_budgets:
        field = sorted(rival_budgets, reverse=True)
        top_rival = field[0] if field else 0
        if budget_remaining > top_rival * 1.5:
            pct *= 0.85
            leverage_note = "you outgun the field - you can win for less"
        elif top_rival > budget_remaining * 1.5:
            pct *= 1.15
            leverage_note = "rivals hold more budget - expect to be outbid"

    pct = min(pct, 0.85)  # never torch the entire budget on one claim
    target = max(1, round(budget_remaining * pct))
    low = max(1, round(target * 0.7))
    high = max(target + 1, round(target * 1.4))

    rationale = f"VOR {vor:.0f}, {player['position']} need is {urgency}"
    for note in (timing_note, leverage_note):
        if note:
            rationale += f"; {note}"

    return {
        "suggested_bid": target,
        "bid_range": [low, high],
        "pct_of_remaining": round(pct * 100),
        "rationale": rationale,
    }


def optimal_lineup(players: list[dict], roster_positions: list[str]) -> dict:
    """Greedy best legal starting lineup from a set of scored players.

    Fills the most constrained slots first (dedicated positions before flex),
    which for standard roster shapes produces the optimal assignment.
    """
    slots = [s for s in roster_positions if s not in scoring.NON_PLAYER_SLOTS]
    pool = sorted(players, key=lambda p: p.get("points") or 0.0, reverse=True)
    used: set[str] = set()
    lineup: list[dict] = []

    def eligible(slot: str, p: dict) -> bool:
        if slot in scoring.FLEX_ELIGIBLE:
            return p["position"] in scoring.FLEX_ELIGIBLE[slot]
        return p["position"] == slot

    # Dedicated slots first, flex last.
    ordered = sorted(slots, key=lambda s: s in scoring.FLEX_ELIGIBLE)

    for slot in ordered:
        pick = next(
            (p for p in pool if p["player_id"] not in used and eligible(slot, p)), None
        )
        if pick:
            used.add(pick["player_id"])
            lineup.append({"slot": slot, **pick})
        else:
            lineup.append({"slot": slot, "name": "— empty —", "points": 0})

    bench = [p for p in pool if p["player_id"] not in used]
    return {
        "lineup": lineup,
        "projected_total": round(sum(p.get("points") or 0.0 for p in lineup), 1),
        "bench": bench,
    }
