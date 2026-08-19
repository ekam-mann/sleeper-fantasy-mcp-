"""Context on a starting lineup - flags, and the calls that are actually close.

The optimiser itself stays deterministic: it fills slots by projected points
and nothing else. That is on purpose. Silently re-ordering a lineup because of
a weather reading or a betting line would make the output unverifiable and hide
the reasoning inside a number nobody can audit.

What was missing is the layer above it. A projection is a mean, and a lineup
decision is made under conditions the projection does not know about: a kicker
in a gale, a receiver whose offence is implied for fourteen points, a starter
listed doubtful. So the models attach as **flags** rather than adjustments.

The second half matters more than the first. A flag on a starter who is twenty
points clear of his backup is noise - nothing about him is a decision. A flag on
a start/sit separated by half a point is the whole ballgame. So flags are paired
with **close calls**: the specific slots where the bench alternative is near
enough that context should decide it.
"""

from __future__ import annotations

# Within this many projected points, a start/sit is close enough that context
# should break the tie rather than the projection.
CLOSE_POINTS = 2.5

INJURY_SERIOUS = {"OUT", "IR", "PUP", "DOUBTFUL", "SUS", "NA"}


def _weather_flag(player: dict, weather_by_team: dict) -> dict | None:
    entry = (weather_by_team or {}).get(player.get("team"))
    if not entry or entry.get("severity", 0) < 1:
        return None
    return {
        "kind": "weather",
        "severity": entry["severity"],
        "detail": entry.get("verdict"),
        "notes": entry.get("notes") or [],
    }


def _vegas_flag(player: dict, env_by_team: dict) -> dict | None:
    env = (env_by_team or {}).get(player.get("team"))
    if not env:
        return None
    implied = env.get("implied_team_total")
    if implied is None:
        return None
    if implied <= 18:
        return {
            "kind": "game_environment",
            "severity": 2,
            "detail": f"low-scoring spot ({implied} implied vs {env.get('opponent')})",
        }
    if implied >= 27:
        return {
            "kind": "game_environment",
            "severity": 0,
            "detail": f"elite spot ({implied} implied vs {env.get('opponent')})",
        }
    return None


def _injury_flag(player: dict) -> dict | None:
    status = (player.get("injury_status") or "").upper()
    if not status:
        return None
    if status in INJURY_SERIOUS:
        return {"kind": "injury", "severity": 3, "detail": f"listed {status.title()}"}
    return {"kind": "injury", "severity": 1, "detail": f"listed {status.title()}"}


def _consistency_flag(player: dict, vol: dict) -> dict | None:
    """Floor vs ceiling, which only matters when a decision is close."""
    v = (vol or {}).get(player.get("player_id"))
    if not v or v.get("cv") is None:
        return None
    if v["cv"] >= 0.75:
        return {
            "kind": "volatility",
            "severity": 1,
            "detail": (
                f"boom/bust (floor {v['floor']}, ceiling {v['ceiling']}) - "
                "prefer him when chasing, avoid when protecting a lead"
            ),
        }
    if v["cv"] <= 0.45:
        return {
            "kind": "volatility",
            "severity": 0,
            "detail": f"steady (floor {v['floor']}) - a safe start",
        }
    return None


def _news_flag(player: dict, signals_by_id: dict) -> dict | None:
    """A validated news signal about this player, marked unverified.

    Kept visibly distinct from the measured flags. Everything else here is
    computed from data the API returned; this one was read out of prose by a
    language model, and a reader should be able to tell the difference at a
    glance rather than having to remember which is which.
    """
    found = (signals_by_id or {}).get(player.get("player_id"))
    if not found:
        return None

    # Worst news first if a player has several.
    rank = {"high": 0, "medium": 1, "low": 2}
    worst = sorted(
        found,
        key=lambda s: (
            s.get("direction") != "negative",
            rank.get(s.get("confidence"), 9),
        ),
    )[0]

    direction = worst.get("direction")
    confidence = worst.get("confidence")
    if direction == "negative":
        severity = {"high": 3, "medium": 2, "low": 1}.get(confidence, 1)
    elif direction == "positive":
        severity = 0
    else:
        severity = 0

    return {
        "kind": "news",
        "severity": severity,
        "verified": False,
        "detail": (
            f"{worst.get('signal_type', 'news')} ({confidence} confidence, "
            f"{direction}): {worst.get('summary')}"
        ),
        "evidence": worst.get("evidence"),
        "caveat": "read from news text, not measured - confirm before acting",
    }


def flags_for(
    player: dict,
    weather_by_team: dict | None = None,
    env_by_team: dict | None = None,
    vol: dict | None = None,
    signals_by_id: dict | None = None,
) -> list[dict]:
    """Everything the models know about this player's week."""
    found = [
        _injury_flag(player),
        _weather_flag(player, weather_by_team or {}),
        _vegas_flag(player, env_by_team or {}),
        _consistency_flag(player, vol or {}),
        _news_flag(player, signals_by_id or {}),
    ]
    out = [f for f in found if f]
    out.sort(key=lambda f: -f["severity"])
    return out


def close_calls(
    lineup: list[dict],
    bench: list[dict],
    weather_by_team: dict | None = None,
    env_by_team: dict | None = None,
    vol: dict | None = None,
    margin: float = CLOSE_POINTS,
    signals_by_id: dict | None = None,
) -> list[dict]:
    """Slots where a bench player is near enough that context should decide.

    This is where flags earn their keep. Everywhere else the projection is
    doing the work and a flag is just noise on the page.
    """
    import ff.scoring as scoring

    out: list[dict] = []
    for entry in lineup:
        slot = entry.get("slot")
        starter_pts = entry.get("points") or 0.0
        if not entry.get("player_id"):
            continue

        eligible_positions = scoring.FLEX_ELIGIBLE.get(slot, (slot,))
        rivals = [
            b
            for b in bench
            if b.get("position") in eligible_positions
            and abs((b.get("points") or 0.0) - starter_pts) <= margin
        ]
        if not rivals:
            continue

        starter_flags = flags_for(
            entry, weather_by_team, env_by_team, vol, signals_by_id
        )
        options = []
        for b in sorted(rivals, key=lambda x: -(x.get("points") or 0.0))[:3]:
            options.append(
                {
                    "name": b["name"],
                    "position": b.get("position"),
                    "team": b.get("team"),
                    "points": b.get("points"),
                    "gap": round((b.get("points") or 0.0) - starter_pts, 1),
                    "flags": flags_for(
                        b, weather_by_team, env_by_team, vol, signals_by_id
                    ),
                }
            )

        out.append(
            {
                "slot": slot,
                "starting": entry.get("name"),
                "starter_points": starter_pts,
                "starter_flags": starter_flags,
                "alternatives": options,
                "why_it_matters": (
                    "The projections are close enough here that the flags, not "
                    "the point estimate, should decide it."
                ),
            }
        )
    return out


def summarise(lineup_flags: list[dict], calls: list[dict]) -> list[str]:
    """The two or three things worth reading before locking a lineup."""
    notes: list[str] = []
    seen: set[tuple] = set()

    def add(flag: dict) -> None:
        # A single flag can match more than one rule below (a severity-3 news
        # item is both "serious" and "bad news"), so dedupe on the flag itself
        # rather than emitting the same line twice.
        key = (flag.get("_player"), flag.get("kind"), flag.get("detail"))
        if key in seen:
            return
        seen.add(key)
        suffix = " [unverified]" if flag.get("verified") is False else ""
        notes.append(f"{flag.get('_player')}: {flag.get('detail')}{suffix}")

    everything = [f for entry in lineup_flags for f in entry["flags"]]

    for f in everything:
        if f["severity"] >= 3:
            add(f)
    for f in everything:
        if f["kind"] in ("weather", "news") and f["severity"] >= 2:
            add(f)

    if calls:
        notes.append(
            f"{len(calls)} start/sit decision(s) are within {CLOSE_POINTS} points - "
            "those are the ones worth thinking about."
        )

    if not notes:
        notes.append(
            "No injury, weather, news, or game-environment concerns in this lineup."
        )
    return notes
