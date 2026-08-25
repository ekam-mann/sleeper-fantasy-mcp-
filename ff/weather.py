"""Game-day weather, and what it actually means for fantasy scoring.

Weather is an in-season lineup tool, not a draft tool: OpenWeather's free
forecast reaches ~5 days out, so a Week 15 forecast simply does not exist in
August. Asking for a distant week returns an honest "too far out" rather than
a fabricated number.

The single most important filter is the roof. Roughly a third of games are
played somewhere weather cannot reach, and for those the correct answer is
"ignore weather entirely" - not a temperature reading.

ESPN publishes an `indoor` boolean, but it is wrong for fantasy purposes at
SoFi (fixed canopy, marked outdoor). So we carry our own roof table and fall
back to ESPN only for venues we do not know.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

import httpx

from . import secrets

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
OWM = "https://api.openweathermap.org/data/2.5/forecast"

# Retractable roofs are treated as closed: teams shut them when weather is bad,
# which is exactly the situation we would otherwise be flagging.
SHELTERED = {"dome", "retractable", "canopy"}

# venue -> (lat, lon, roof). Keys are accent-stripped lowercase.
STADIUMS: dict[str, tuple[float, float, str]] = {
    "at&t stadium": (32.7473, -97.0945, "retractable"),
    "acrisure stadium": (40.4468, -80.0158, "outdoor"),
    "allegiant stadium": (36.0909, -115.1833, "dome"),
    "bank of america stadium": (35.2258, -80.8528, "outdoor"),
    "caesars superdome": (29.9511, -90.0812, "dome"),
    "empower field at mile high": (39.7439, -105.0201, "outdoor"),
    "estadio banorte": (19.3029, -99.1505, "outdoor"),
    "everbank stadium": (30.3239, -81.6373, "outdoor"),
    "fc bayern munich stadium": (48.2188, 11.6247, "outdoor"),
    "ford field": (42.3400, -83.0456, "dome"),
    "geha field at arrowhead stadium": (39.0489, -94.4839, "outdoor"),
    "gillette stadium": (42.0909, -71.2643, "outdoor"),
    "hard rock stadium": (25.9580, -80.2389, "outdoor"),
    "highmark stadium": (42.7738, -78.7870, "outdoor"),
    "huntington bank field": (41.5061, -81.6995, "outdoor"),
    "lambeau field": (44.5013, -88.0622, "outdoor"),
    "levi's stadium": (37.4033, -121.9694, "outdoor"),
    "lincoln financial field": (39.9008, -75.1675, "outdoor"),
    "lucas oil stadium": (39.7601, -86.1639, "retractable"),
    "lumen field": (47.5952, -122.3316, "outdoor"),
    "m&t bank stadium": (39.2780, -76.6227, "outdoor"),
    "maracana stadium": (-22.9121, -43.2302, "outdoor"),
    "melbourne cricket ground": (-37.8200, 144.9834, "outdoor"),
    "mercedes-benz stadium": (33.7554, -84.4008, "retractable"),
    "metlife stadium": (40.8135, -74.0745, "outdoor"),
    "nrg stadium": (29.6847, -95.4107, "retractable"),
    "nissan stadium": (36.1665, -86.7713, "outdoor"),
    "northwest stadium": (38.9077, -76.8645, "outdoor"),
    "paycor stadium": (39.0955, -84.5161, "outdoor"),
    "raymond james stadium": (27.9759, -82.5033, "outdoor"),
    "santiago bernabeu": (40.4531, -3.6883, "retractable"),
    # ESPN says outdoor; the fixed canopy means weather is a non-factor.
    "sofi stadium": (33.9535, -118.3392, "canopy"),
    "soldier field": (41.8623, -87.6167, "outdoor"),
    "stade de france": (48.9245, 2.3601, "outdoor"),
    "state farm stadium": (33.5277, -112.2626, "retractable"),
    "tottenham hotspur stadium": (51.6043, -0.0665, "outdoor"),
    "u.s. bank stadium": (44.9738, -93.2578, "dome"),
    "wembley stadium": (51.5560, -0.2796, "outdoor"),
}


# ESPN is not consistent about venue names - it serves current names, former
# names, and sponsor-free short forms interchangeably across weeks. An
# unrecognised name silently drops that game from the forecast, so known
# variants map onto the canonical key rather than being missed.
VENUE_ALIASES = {
    "reliant stadium": "nrg stadium",
    "arrowhead stadium": "geha field at arrowhead stadium",
    "mercedes-benz superdome": "caesars superdome",
    "superdome": "caesars superdome",
    "fedexfield": "northwest stadium",
    "commanders field": "northwest stadium",
    "heinz field": "acrisure stadium",
    "firstenergy stadium": "huntington bank field",
    "cleveland browns stadium": "huntington bank field",
    "tiaa bank field": "everbank stadium",
    "jacksonville municipal stadium": "everbank stadium",
    "new era field": "highmark stadium",
    "ralph wilson stadium": "highmark stadium",
    "nissan stadium at the east bank": "nissan stadium",
    "sports authority field at mile high": "empower field at mile high",
    "univision stadium": "estadio banorte",
    "estadio azteca": "estadio banorte",
    "allianz arena": "fc bayern munich stadium",
    "santiago bernabeu stadium": "santiago bernabeu",
}


def _norm(name: str) -> str:
    """Accent- and case-insensitive key (Maracana, Bernabeu)."""
    s = unicodedata.normalize("NFKD", name or "")
    key = "".join(c for c in s if not unicodedata.combining(c)).strip().lower()
    return VENUE_ALIASES.get(key, key)


def games(season: str, week: int) -> list[dict]:
    """Kickoff time and venue for each game in a week."""
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        resp = client.get(ESPN, params={"dates": season, "seasontype": 2, "week": week})
        resp.raise_for_status()
        data = resp.json()

    out = []
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            venue = comp.get("venue") or {}
            teams = [
                (c.get("team") or {}).get("abbreviation")
                for c in comp.get("competitors", [])
            ]
            out.append(
                {
                    "kickoff_utc": event.get("date"),
                    "venue": venue.get("fullName"),
                    "city": (venue.get("address") or {}).get("city"),
                    "espn_indoor": venue.get("indoor"),
                    "teams": [t for t in teams if t],
                }
            )
    return out


def _roof(game: dict) -> str:
    entry = STADIUMS.get(_norm(game.get("venue") or ""))
    if entry:
        return entry[2]
    return "dome" if game.get("espn_indoor") else "outdoor"


def _matchup(game: dict) -> str:
    teams = game.get("teams") or []
    # ESPN lists the home team first; fantasy convention is "away @ home".
    return " @ ".join(reversed(teams)) if len(teams) == 2 else "?"


def _forecast_at(lat: float, lon: float, kickoff: datetime, key: str) -> dict | None:
    """Nearest 3-hour forecast slot to kickoff, or None if out of range."""
    with httpx.Client(timeout=25.0) as client:
        resp = client.get(
            OWM, params={"lat": lat, "lon": lon, "appid": key, "units": "imperial"}
        )
        if resp.status_code == 401:
            raise PermissionError(
                "OpenWeather rejected the API key (401). A newly created key can "
                "take up to a couple of hours to activate."
            )
        resp.raise_for_status()
        slots = resp.json().get("list") or []

    if not slots:
        return None

    best = min(
        slots,
        key=lambda s: abs(datetime.fromtimestamp(s["dt"], tz=timezone.utc) - kickoff),
    )
    gap = abs(datetime.fromtimestamp(best["dt"], tz=timezone.utc) - kickoff)
    if gap.total_seconds() > 3 * 3600:
        return None  # kickoff is outside the forecast window

    wind = best.get("wind") or {}
    return {
        "temp_f": best["main"]["temp"],
        "feels_like_f": best["main"].get("feels_like"),
        "wind_mph": round(wind.get("speed", 0), 1),
        "wind_gust_mph": round(wind["gust"], 1) if wind.get("gust") else None,
        "conditions": (best.get("weather") or [{}])[0].get("description"),
        "precip_chance": round((best.get("pop") or 0) * 100),
        "forecast_time_utc": best.get("dt_txt"),
    }


def fantasy_impact(wx: dict) -> dict:
    """Translate a forecast into positional guidance.

    Wind is the dominant variable - it degrades the deep passing game and
    kicking far more than cold or rain do. Rain and cold mostly matter through
    ball security and by nudging play-calling toward the run.
    """
    notes: list[str] = []
    severity = 0

    wind = wx.get("wind_mph") or 0
    gust = wx.get("wind_gust_mph") or 0
    effective = max(wind, gust * 0.8)

    if effective >= 25:
        severity = 3
        notes.append(
            f"severe wind ({wind:.0f} mph, gusts {gust:.0f}) - deep passing and "
            "kicking badly degraded"
        )
    elif effective >= 18:
        severity = max(severity, 2)
        notes.append(
            f"strong wind ({wind:.0f} mph) - downgrade deep threats and kickers"
        )
    elif effective >= 13:
        severity = max(severity, 1)
        notes.append(
            f"breezy ({wind:.0f} mph) - mild drag on the vertical passing game"
        )

    cond = (wx.get("conditions") or "").lower()
    pop = wx.get("precip_chance") or 0
    if "snow" in cond:
        severity = max(severity, 2)
        notes.append("snow - expect a run-heavier script and ball-security issues")
    elif ("rain" in cond or "thunder" in cond) and pop >= 50:
        severity = max(severity, 1)
        notes.append(
            f"rain likely ({pop}%) - modest drag on passing volume and efficiency"
        )

    temp = wx.get("temp_f")
    if temp is not None and temp <= 20:
        severity = max(severity, 1)
        notes.append(f"frigid ({temp:.0f}F) - historically suppresses scoring")

    verdict = {
        0: "clean conditions - no weather adjustment",
        1: "minor - a tiebreaker between close options only",
        2: "meaningful - downgrade passing games, favor RBs",
        3: "severe - avoid kickers and deep passing games entirely",
    }[severity]

    return {"severity": severity, "verdict": verdict, "notes": notes}


def week_weather(season: str, week: int) -> dict:
    """Forecast plus fantasy read for every game in a week."""
    key = secrets.openweather_key()
    if not key:
        return {
            "error": "No OpenWeather API key configured "
            "(secrets.json or OPENWEATHER_API_KEY)."
        }

    results: list[dict] = []
    sheltered: list[dict] = []
    unavailable: list[dict] = []
    auth_error = None

    for g in games(season, week):
        roof = _roof(g)
        entry = STADIUMS.get(_norm(g.get("venue") or ""))
        matchup = _matchup(g)

        if roof in SHELTERED:
            sheltered.append({"matchup": matchup, "venue": g["venue"], "roof": roof})
            continue

        if not entry:
            unavailable.append(
                {
                    "matchup": matchup,
                    "venue": g["venue"],
                    "reason": "unknown venue coordinates",
                }
            )
            continue

        try:
            kickoff = datetime.fromisoformat(g["kickoff_utc"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            unavailable.append(
                {"matchup": matchup, "venue": g["venue"], "reason": "no kickoff time"}
            )
            continue

        try:
            wx = _forecast_at(entry[0], entry[1], kickoff, key)
        except PermissionError as e:
            auth_error = str(e)
            break
        except Exception as e:
            unavailable.append(
                {"matchup": matchup, "venue": g["venue"], "reason": str(e)[:80]}
            )
            continue

        if wx is None:
            unavailable.append(
                {
                    "matchup": matchup,
                    "venue": g["venue"],
                    "reason": "kickoff beyond the ~5-day forecast window",
                }
            )
            continue

        results.append(
            {
                "matchup": matchup,
                "venue": g["venue"],
                "kickoff_utc": g["kickoff_utc"],
                **wx,
                **fantasy_impact(wx),
            }
        )

    if auth_error:
        return {"error": auth_error, "season": season, "week": week}

    results.sort(key=lambda r: r["severity"], reverse=True)
    return {
        "season": season,
        "week": week,
        "games_with_forecast": results,
        "weatherproof_games": sheltered,
        "no_forecast": unavailable,
        "note": (
            "Forecasts reach about 5 days out. Games further away appear under "
            "no_forecast until their week approaches."
        ),
    }
