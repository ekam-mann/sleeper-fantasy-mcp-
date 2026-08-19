"""Expected fantasy points - what the opportunity was worth, before talent.

xFP answers a different question from a projection. A projection asks "how many
points will he score?". xFP asks "how many points would a *league-average*
player have scored with exactly his opportunities?" The gap between actual and
expected is the part attributable to efficiency and touchdown luck - and that
part regresses hard year over year, while opportunity is sticky.

So a receiver who beat his xFP by 60 points is not necessarily good; he is
likely to be cheaper next year than his box score suggests he should be, and
likely to disappoint at that price. A back who trailed his xFP is the opposite:
the role is real, the results were not, and the market usually underrates him.

**Only genuine opportunity is used as an input.** Targets, carries, and their
red-zone subsets. Sleeper also publishes reception buckets (`rec_0_4`,
`rec_10_19`, ...) but those count receptions by yards *gained* - they are
outcomes, not opportunities, and feeding them in would leak the answer into the
model and make every player look perfectly predicted.

The weights are not hardcoded. They are fitted by least squares against the
league's own scoring, separately per position, so a red-zone carry is worth
what it is actually worth in *your* league rather than what it is worth in
some generic one.
"""

from __future__ import annotations

from . import scoring as scoring_mod
from . import sleeper

POSITIONS = ["QB", "RB", "WR", "TE"]

# Minimum opportunities before a player is used to fit the model. Low-volume
# players are mostly noise and would drag the coefficients around.
MIN_OPPORTUNITY = 25

TTL = 7 * 24 * 3600

# Feature layout per position: (label, non-red-zone field, red-zone field)
FEATURES = {
    "QB": [("pass attempt", "pass_att", "pass_rz_att"), ("carry", "rush_att", "rush_rz_att")],
    "RB": [("target", "rec_tgt", "rec_rz_tgt"), ("carry", "rush_att", "rush_rz_att")],
    "WR": [("target", "rec_tgt", "rec_rz_tgt"), ("carry", "rush_att", "rush_rz_att")],
    "TE": [("target", "rec_tgt", "rec_rz_tgt"), ("carry", "rush_att", "rush_rz_att")],
}


def _season_rows(season: str) -> list[dict]:
    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    url = (
        f"https://api.sleeper.app/stats/nfl/{season}"
        f"?season_type=regular&{qs}&order_by=pts_ppr"
    )
    try:
        return sleeper._get(url, f"stats_season_{season}", TTL)
    except Exception:
        return []


def _design_row(stats: dict, position: str) -> list[float]:
    """Split each opportunity type into non-red-zone and red-zone components.

    Red zone is separated because a carry from the two-yard line and a carry
    from midfield are wildly different in expected points, and a model that
    averages them will systematically undervalue goal-line backs.
    """
    row: list[float] = []
    for _, plain_field, rz_field in FEATURES[position]:
        total = stats.get(plain_field) or 0.0
        rz = stats.get(rz_field) or 0.0
        rz = min(rz, total)  # guard against inconsistent source data
        row.extend([total - rz, rz])
    return row


def _solve(xtx: list[list[float]], xty: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. Avoids a numpy dependency."""
    n = len(xty)
    aug = [row[:] + [xty[i]] for i, row in enumerate(xtx)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-9:
            return None  # singular; not enough independent variation
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / aug[col][col]
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]

    return [aug[i][n] / aug[i][i] for i in range(n)]


def _normal_equations(
    samples: list[tuple[list[float], float]], cols: list[int]
) -> tuple[list[list[float]], list[float]]:
    """Build (X'X, X'y) over a subset of feature columns."""
    k = len(cols)
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for x, y in samples:
        for i, ci in enumerate(cols):
            xty[i] += x[ci] * y
            for j, cj in enumerate(cols):
                xtx[i][j] += x[ci] * x[cj]
    return xtx, xty


def _fit_nonnegative(
    samples: list[tuple[list[float], float]], k: int
) -> list[float] | None:
    """Least squares constrained to non-negative coefficients.

    Plain OLS happily returns a negative weight for a sparse, collinear column -
    red-zone targets for running backs, for instance, where the column is small
    and correlated with total targets. A negative expected value for an
    opportunity is not a subtle statistical quirk, it is impossible: it would
    mean a goal-line carry actively costs you points, and would drag down the
    xFP of exactly the players the metric exists to identify.

    So this runs an active-set pass: fit, and if any coefficient comes back
    negative, pin the worst offender to zero and refit on what remains. With
    only four features this converges in a couple of iterations.
    """
    active = list(range(k))
    while active:
        xtx, xty = _normal_equations(samples, active)
        solved = _solve(xtx, xty)
        if solved is None:
            return None

        worst = min(range(len(solved)), key=lambda i: solved[i])
        if solved[worst] >= 0:
            beta = [0.0] * k
            for i, ci in enumerate(active):
                beta[ci] = solved[i]
            return beta

        active.pop(worst)  # pin it to zero and refit

    return [0.0] * k


def fit_weights(
    season: str, scoring: dict[str, float]
) -> dict[str, dict]:
    """Fit points-per-opportunity weights per position, in league scoring."""
    rows = _season_rows(season)
    by_pos: dict[str, list[tuple[list[float], float]]] = {}

    for r in rows:
        pos = (r.get("player") or {}).get("position")
        stats = r.get("stats") or {}
        if pos not in FEATURES or not stats.get("gp"):
            continue
        x = _design_row(stats, pos)
        if sum(x) < MIN_OPPORTUNITY:
            continue
        y = scoring_mod.score_stats(stats, scoring)
        by_pos.setdefault(pos, []).append((x, y))

    out: dict[str, dict] = {}
    for pos, samples in by_pos.items():
        k = len(samples[0][0])
        beta = _fit_nonnegative(samples, k)
        if beta is None:
            continue

        labels: list[str] = []
        for label, _, _ in FEATURES[pos]:
            labels.extend([label, f"red-zone {label}"])

        out[pos] = {
            "weights": {lab: round(b, 3) for lab, b in zip(labels, beta)},
            "beta": beta,
            "sample": len(samples),
        }
    return out


def xfp_table(season: str, scoring: dict[str, float]) -> dict[str, dict]:
    """Expected vs actual fantasy points for every player, league-scored."""
    fitted = fit_weights(season, scoring)
    rows = _season_rows(season)
    out: dict[str, dict] = {}

    for r in rows:
        pid = r.get("player_id")
        pos = (r.get("player") or {}).get("position")
        stats = r.get("stats") or {}
        if not pid or pos not in fitted or not stats.get("gp"):
            continue

        x = _design_row(stats, pos)
        opportunities = sum(x)
        if opportunities <= 0:
            continue

        beta = fitted[pos]["beta"]
        expected = sum(a * b for a, b in zip(x, beta))
        actual = scoring_mod.score_stats(stats, scoring)
        games = stats.get("gp") or 1

        out[pid] = {
            "season": season,
            "position": pos,
            "games": games,
            "opportunities": round(opportunities, 1),
            "opportunities_per_game": round(opportunities / games, 1),
            "expected_points": round(expected, 1),
            "actual_points": round(actual, 1),
            "delta": round(actual - expected, 1),
            "delta_per_game": round((actual - expected) / games, 2),
            "efficiency_ratio": round(actual / expected, 2) if expected > 0 else None,
            "xfp_per_game": round(expected / games, 1),
        }

    return out


def regression_read(x: dict | None) -> list[str]:
    """What the gap between actual and expected implies for next season."""
    if not x:
        return ["no opportunity data (rookie, or did not play)"]

    notes: list[str] = []
    ratio = x.get("efficiency_ratio")
    per_game = x.get("delta_per_game") or 0

    if ratio is not None:
        if ratio >= 1.25:
            notes.append(
                f"scored {ratio:.2f}x his opportunity ({per_game:+.1f} pts/game above "
                "expected) - efficiency and TD luck regress, so expect a step back "
                "unless the role grows"
            )
        elif ratio >= 1.10:
            notes.append(f"modestly outperformed his opportunity ({ratio:.2f}x)")
        elif ratio <= 0.80:
            notes.append(
                f"scored only {ratio:.2f}x his opportunity ({per_game:+.1f} pts/game) - "
                "the role is real even though the results were not; a classic "
                "bounce-back profile"
            )
        elif ratio <= 0.92:
            notes.append(f"slightly underperformed his opportunity ({ratio:.2f}x)")
        else:
            notes.append(f"scored about in line with his opportunity ({ratio:.2f}x)")

    opg = x.get("opportunities_per_game")
    if opg is not None:
        if opg >= 18:
            notes.append(f"heavy workload ({opg}/game) - opportunity is sticky, this is the signal")
        elif opg <= 6:
            notes.append(f"thin workload ({opg}/game) - little to build on")

    notes.append(
        f"expected {x['expected_points']} vs actual {x['actual_points']} points"
    )
    return notes
