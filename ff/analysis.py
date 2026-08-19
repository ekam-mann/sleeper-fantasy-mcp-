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


def _need_multiplier(urgency: str) -> float:
    return {
        "critical": 1.15,
        "thin": 1.05,
        "adequate": 1.0,
        "surplus": 0.88,
    }.get(urgency, 1.0)


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

    scored: list[dict] = []
    for p in available:
        mult = _need_multiplier(needs.get(p["position"], {}).get("urgency", "adequate"))
        adjusted = p["vor"] * mult

        adp = p.get("adp")
        if adp and pick_number:
            # Positive = he has fallen past his ADP and is a bargain here.
            adp_delta = adp - pick_number
        else:
            adp_delta = None

        reasons = []
        need_info = needs.get(p["position"], {})
        if need_info.get("urgency") == "critical":
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
                "adp_delta": round(adp_delta, 1) if adp_delta is not None else None,
                "why": "; ".join(reasons) or "best value on the board",
            }
        )

    scored.sort(key=lambda r: r["adjusted_value"], reverse=True)
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
