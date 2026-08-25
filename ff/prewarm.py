"""Build the expensive tables in the background at startup.

The first question of a session was paying to construct eight derived tables -
usage, form, volatility, availability, xFP and the defensive ranks - roughly
seven seconds before it could answer anything. Every question after it was
fast, because the memo already held them.

That cost is not avoidable, but it does not have to be *paid by the user*. The
server spends several seconds importing `mcp` and `httpx` at startup anyway,
and it sits idle after that waiting for a first request. Building the tables on
a daemon thread during that idle window moves the cost off the critical path.

Deliberately quiet: any failure here is swallowed. A prewarm that breaks the
server it was meant to speed up is strictly worse than no prewarm, and every
table it touches is rebuilt on demand anyway if this never finishes.
"""

from __future__ import annotations

import threading

_STARTED = False
_LOCK = threading.Lock()


def _build(league_ids: list[str]) -> None:
    from . import (
        availability,
        context,
        form,
        scoring,
        sleeper,
        sos,
        usage,
        volatility,
        xfp,
    )

    for lid in league_ids:
        try:
            lg = sleeper.league(lid)
            season = int(lg.get("season") or sleeper.nfl_state()["season"])
            prior = str(season - 1)
            sc = lg.get("scoring_settings")

            # Cheapest first, so an interrupted prewarm still leaves the most
            # widely shared tables built.
            usage.usage_table(prior)
            sos.points_allowed(prior, sc)
            sos.defense_ranks(prior, sc)
            xfp.fit_weights(prior, sc)
            xfp.xfp_table(prior, sc)
            form.form_table(prior, sc)
            try:
                startable = scoring.starter_counts(*context.league_shape(lg))
                volatility.volatility_table(prior, sc, startable)
            except Exception:
                pass  # league shape unavailable; skip the shaped variant
            availability.availability_table(
                [str(y) for y in range(2021, season)]
            )
        except Exception:
            continue


def start(league_ids: list[str]) -> bool:
    """Kick off the prewarm once. Returns whether this call started it."""
    global _STARTED
    with _LOCK:
        if _STARTED or not league_ids:
            return False
        _STARTED = True

    threading.Thread(
        target=_build,
        args=(list(league_ids),),
        name="ff-prewarm",
        daemon=True,  # must never hold the process open
    ).start()
    return True
