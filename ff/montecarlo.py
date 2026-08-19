"""Monte Carlo season simulation - turning a point estimate into a distribution.

A projection of 250 points is a mean, and a mean hides the two things that
actually decide a fantasy season:

  - **Availability.** A player who misses five games scores nothing in those
    five games, and no amount of per-game excellence recovers it.
  - **Weekly variance.** The same season total can arrive as sixteen steady
    games or as four huge ones and twelve duds, and those are different assets.

So we simulate rather than assert. Each run samples a games-played count from
the player's own availability history, then samples a score for each of those
games from a distribution calibrated to his projection and his historical
volatility. Repeat a few thousand times and you get the shape of the outcome,
not just its centre.

Weekly scores are drawn from a gamma distribution: non-negative (you cannot
score -3), right-skewed (occasional huge weeks, no symmetric huge-negative
counterpart), and fully specified by a mean and a coefficient of variation,
which is exactly what the volatility module already produces.

This is a model, not a forecast. It inherits every bias in the underlying
projection - it only adds the shape that the projection omits.
"""

from __future__ import annotations

import random
import statistics

GAMES_PER_SEASON = 17

# Fallback volatility when a player has no weekly history (rookies). Set to a
# typical value for the position rather than something artificially tidy.
DEFAULT_CV = {"QB": 0.45, "RB": 0.60, "WR": 0.65, "TE": 0.70, "DEF": 0.75, "K": 0.50}


def _sample_weekly(mean: float, cv: float, rng: random.Random) -> float:
    """One week's score from a gamma with the given mean and CV."""
    if mean <= 0:
        return 0.0
    if cv <= 0.01:
        return mean
    # Gamma parameterised by mean and CV: shape = 1/cv^2, scale = mean*cv^2.
    shape = 1.0 / (cv * cv)
    scale = mean * cv * cv
    return rng.gammavariate(shape, scale)


def simulate_player(
    projected_points: float,
    position: str,
    cv: float | None = None,
    availability_pct: float | None = None,
    runs: int = 4000,
    seed: int | None = None,
) -> dict:
    """Simulate a full season for one player.

    `projected_points` is a full-season projection, which already has some
    expected missed time baked in. We convert it to a per-game rate over the
    games he is expected to play, then re-apply availability explicitly - so
    the downside of an injury-prone player shows up in the spread rather than
    being silently averaged away.
    """
    rng = random.Random(seed)
    cv = cv if cv is not None else DEFAULT_CV.get(position, 0.65)
    avail = (availability_pct or 100.0) / 100.0
    avail = min(max(avail, 0.05), 1.0)

    expected_games = max(1.0, GAMES_PER_SEASON * avail)
    per_game = projected_points / expected_games

    totals: list[float] = []
    games_played: list[int] = []

    for _ in range(runs):
        played = sum(1 for _ in range(GAMES_PER_SEASON) if rng.random() < avail)
        total = sum(_sample_weekly(per_game, cv, rng) for _ in range(played))
        totals.append(total)
        games_played.append(played)

    totals.sort()

    def pct(p: float) -> float:
        if not totals:
            return 0.0
        k = (len(totals) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(totals) - 1)
        return round(totals[lo] + (totals[hi] - totals[lo]) * (k - lo), 1)

    return {
        "runs": runs,
        "projected_points": round(projected_points, 1),
        "assumed_cv": round(cv, 2),
        "assumed_availability_pct": round(avail * 100, 1),
        "mean": round(statistics.mean(totals), 1) if totals else 0.0,
        "p10": pct(0.10),
        "p25": pct(0.25),
        "median": pct(0.50),
        "p75": pct(0.75),
        "p90": pct(0.90),
        "expected_games": round(statistics.mean(games_played), 1),
        "bust_risk_pct": round(
            100 * sum(1 for t in totals if t < projected_points * 0.7) / len(totals)
        )
        if totals
        else None,
        "smash_chance_pct": round(
            100 * sum(1 for t in totals if t > projected_points * 1.3) / len(totals)
        )
        if totals
        else None,
    }


def compare(sims: list[dict], names: list[str]) -> dict:
    """Head-to-head read on two or more simulated players."""
    if len(sims) < 2:
        return {}
    ranked = sorted(
        zip(names, sims), key=lambda x: x[1]["median"], reverse=True
    )
    best_floor = max(zip(names, sims), key=lambda x: x[1]["p10"])
    best_ceiling = max(zip(names, sims), key=lambda x: x[1]["p90"])

    return {
        "highest_median": ranked[0][0],
        "safest_floor": best_floor[0],
        "highest_ceiling": best_ceiling[0],
        "note": (
            "Take the floor in weekly head-to-head, the ceiling in best ball. "
            "When they disagree, the choice is about format, not about talent."
        ),
    }


def outcome_read(sim: dict, position: str) -> list[str]:
    """Plain-language summary of a simulated distribution."""
    notes: list[str] = []
    spread = sim["p90"] - sim["p10"]
    mid = sim["median"] or 1

    if spread / mid >= 1.1:
        notes.append(
            f"very wide range of outcomes ({sim['p10']}-{sim['p90']}) - a gamble"
        )
    elif spread / mid <= 0.55:
        notes.append(
            f"tight range ({sim['p10']}-{sim['p90']}) - you know what you're getting"
        )

    if sim["expected_games"] < 15:
        notes.append(
            f"expected to miss time - {sim['expected_games']} games in the average season"
        )

    if (sim.get("bust_risk_pct") or 0) >= 35:
        notes.append(f"{sim['bust_risk_pct']}% chance he finishes 30% below projection")
    if (sim.get("smash_chance_pct") or 0) >= 25:
        notes.append(f"{sim['smash_chance_pct']}% chance he beats projection by 30%+")

    notes.append(
        f"floor {sim['p10']} / median {sim['median']} / ceiling {sim['p90']} points"
    )
    return notes
