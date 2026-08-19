"""Betting lines, and the game scripts they imply.

The betting market is the sharpest public forecast of how a game will go, and
ESPN republishes it for free. Two numbers carry nearly all the fantasy signal:

  total   - how many points the game is expected to produce
  spread  - how those points are expected to split

From those you get each team's *implied total*, which is a far better read on
a team's scoring environment than any projection of a single player:

    favorite = total/2 + |spread|/2
    underdog = total/2 - |spread|/2

The common folk wisdom - "target underdogs for garbage time" - is mostly
wrong. Trailing teams throw more, but they also run fewer total plays and
score less. Players on favored, high-implied-total teams score more in
aggregate because the pie is bigger. Volume shifts at the margin; pie size
dominates. This module leans on pie size and treats script as a modifier.
"""

from __future__ import annotations

import httpx

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

TEAM_FIXUPS = {"WSH": "WAS"}


def _fix(t: str | None) -> str | None:
    return TEAM_FIXUPS.get(t, t) if t else None


def _pick_line(odds: list[dict]) -> dict | None:
    """Prefer the provider ESPN ranks first; fall back to any usable line."""
    if not odds:
        return None
    ranked = sorted(
        odds, key=lambda o: (o.get("provider") or {}).get("priority", 99)
    )
    for o in ranked:
        if o.get("overUnder") is not None or o.get("spread") is not None:
            return o
    return None


def week_odds(season: str, week: int) -> list[dict]:
    """Spread, total, and implied team totals for each game in a week."""
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        resp = client.get(ESPN, params={"dates": season, "seasontype": 2, "week": week})
        resp.raise_for_status()
        data = resp.json()

    games: list[dict] = []
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            line = _pick_line(comp.get("odds") or [])
            if not line:
                continue

            home = away = None
            for c in comp.get("competitors", []):
                abbr = _fix((c.get("team") or {}).get("abbreviation"))
                if c.get("homeAway") == "home":
                    home = abbr
                elif c.get("homeAway") == "away":
                    away = abbr
            if not home or not away:
                continue

            total = line.get("overUnder")
            spread = line.get("spread")

            # ESPN's `spread` is stated from the home team's perspective.
            fav = None
            if spread is not None:
                fav = home if spread < 0 else away if spread > 0 else None

            implied = _implied_totals(total, spread, home, away)

            games.append(
                {
                    "kickoff_utc": event.get("date"),
                    "away": away,
                    "home": home,
                    "matchup": f"{away} @ {home}",
                    "total": total,
                    "spread": spread,
                    "spread_detail": line.get("details"),
                    "favorite": fav,
                    "provider": (line.get("provider") or {}).get("displayName"),
                    "implied_totals": implied,
                }
            )

    return games


def _implied_totals(
    total: float | None, spread: float | None, home: str, away: str
) -> dict[str, float]:
    if total is None or spread is None:
        return {}
    margin = abs(spread) / 2
    half = total / 2
    # 2dp, not 1: spreads land on .25/.75 often enough that rounding to a
    # single decimal makes the two sides stop summing to the game total.
    if spread < 0:  # home favored
        return {home: round(half + margin, 2), away: round(half - margin, 2)}
    return {away: round(half + margin, 2), home: round(half - margin, 2)}


def team_environment(team: str, season: str, week: int) -> dict | None:
    """The scoring environment one team is walking into this week."""
    team = _fix(team)
    for g in week_odds(season, week):
        if team not in (g["home"], g["away"]):
            continue
        opp = g["away"] if team == g["home"] else g["home"]
        implied = (g.get("implied_totals") or {}).get(team)
        is_fav = g.get("favorite") == team
        margin = abs(g["spread"]) if g.get("spread") is not None else None

        return {
            "team": team,
            "opponent": opp,
            "home": team == g["home"],
            "game_total": g["total"],
            "spread": g["spread"],
            "favorite": is_fav,
            "implied_team_total": implied,
            "script": _script(implied, is_fav, margin, g["total"]),
        }
    return None


def _script(
    implied: float | None, is_fav: bool, margin: float | None, total: float | None
) -> dict:
    """Read the likely game flow, and who it helps."""
    if implied is None or margin is None:
        return {"read": "no line posted yet", "notes": []}

    notes: list[str] = []

    # Pie size first - this is the dominant term.
    if implied >= 27:
        notes.append(f"elite scoring environment ({implied} implied) - start everyone startable")
    elif implied >= 23:
        notes.append(f"good scoring environment ({implied} implied)")
    elif implied <= 17:
        notes.append(f"weak scoring environment ({implied} implied) - downgrade across the board")

    # Then script as a modifier.
    if is_fav and margin >= 7:
        notes.append("heavy favorite - positive script favors the RB, especially late and near the goal line")
    elif not is_fav and margin >= 7:
        notes.append("heavy underdog - pass volume rises, but on fewer plays; RB loses carries")
    elif total is not None and total >= 48 and margin <= 3:
        notes.append("high total, tight spread - shootout profile, best case for pass catchers")

    if total is not None and total <= 38:
        notes.append("low total - a game to avoid for anyone but the clear workhorse")

    if implied >= 24 and is_fav:
        read = "strong - favored offense with a real pie"
    elif implied <= 17:
        read = "avoid - little scoring expected"
    elif not is_fav and margin >= 7:
        read = "mixed - volume up, efficiency and scoring down"
    else:
        read = "neutral"

    return {"read": read, "notes": notes}


def best_and_worst(season: str, week: int, limit: int = 5) -> dict:
    """Rank every team's scoring environment for the week."""
    envs: list[dict] = []
    for g in week_odds(season, week):
        for team in (g["home"], g["away"]):
            implied = (g.get("implied_totals") or {}).get(team)
            if implied is None:
                continue
            envs.append(
                {
                    "team": team,
                    "opponent": g["away"] if team == g["home"] else g["home"],
                    "implied_total": implied,
                    "game_total": g["total"],
                    "favorite": g.get("favorite") == team,
                }
            )

    envs.sort(key=lambda e: e["implied_total"], reverse=True)
    return {
        "season": season,
        "week": week,
        "best_environments": envs[:limit],
        "worst_environments": envs[-limit:][::-1],
        "note": "Implied team total = total/2 +/- spread/2. Higher means a bigger pie to share.",
    }
