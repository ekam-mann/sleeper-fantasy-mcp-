"""Shared league context: config, player values, and identity resolution."""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path

from . import schedule, scoring, sleeper

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        cfg = {}
    # Env vars win, so you can point the server at a league without editing files.
    if os.getenv("SLEEPER_USERNAME"):
        cfg["username"] = os.environ["SLEEPER_USERNAME"]
    if os.getenv("SLEEPER_LEAGUE_ID"):
        cfg["default_league_id"] = os.environ["SLEEPER_LEAGUE_ID"]
    return cfg


class ConfigMissing(RuntimeError):
    """Raised when there is nothing to work with, with instructions attached."""


def _require_config(cfg: dict) -> None:
    """Fail loudly and usefully rather than returning empty results.

    A fresh clone has no config.json. Silently returning nothing sends a new
    user hunting through the code for a bug that is really a setup step.
    """
    if cfg.get("default_league_id") or cfg.get("leagues"):
        return
    raise ConfigMissing(
        f"No league configured. Copy config.example.json to "
        f"{CONFIG_PATH.name} and add your Sleeper league ID "
        "(find it in your league URL: sleeper.app/leagues/<league_id>/team), "
        "or set SLEEPER_LEAGUE_ID in the environment."
    )


def resolve_league_id(league_id: str | None) -> str:
    cfg = load_config()
    if not league_id:
        _require_config(cfg)
    if league_id:
        # Allow friendly names from config, e.g. "main".
        for lg in cfg.get("leagues", []):
            if lg.get("name", "").lower() == league_id.lower():
                return lg["league_id"]
        return league_id
    default = cfg.get("default_league_id")
    if not default:
        leagues = cfg.get("leagues", [])
        if leagues:
            return leagues[0]["league_id"]
        raise ValueError(
            "No league_id given and no default configured. Add one to config.json "
            "or pass league_id explicitly."
        )
    return default


def my_roster_id(league_id: str) -> int | None:
    """Find the caller's roster_id from the configured Sleeper username."""
    cfg = load_config()
    username = cfg.get("username")
    if not username:
        return None
    users = sleeper.league_users(league_id)
    me = next(
        (u for u in users if u.get("display_name", "").lower() == username.lower()), None
    )
    if not me:
        return None
    rosters = sleeper.league_rosters(league_id)
    mine = next((r for r in rosters if r.get("owner_id") == me["user_id"]), None)
    return mine.get("roster_id") if mine else None


def league_values(league_id: str, week: int | None = None) -> tuple[dict, list[dict]]:
    """Return (league, player_values) with projections scored for this league.

    week=None gives rest-of-season/full-season values; an int gives that week.
    """
    lg = sleeper.league(league_id)
    season = lg.get("season") or sleeper.nfl_state()["season"]

    if week is None:
        projections = sleeper.season_projections(season)
    else:
        projections = sleeper.week_projections(season, week)

    rows = scoring.build_player_values(
        projections,
        lg.get("scoring_settings") or {},
        lg.get("roster_positions") or [],
        league_shape(lg)[1],
    )
    scoring.positional_ranks(rows)
    _enrich(rows, season)
    return lg, rows


def _enrich(rows: list[dict], season: str) -> None:
    """Fill in bye week, injury detail, and depth chart position.

    Byes come from ESPN because Sleeper publishes none; the rest comes from
    Sleeper's own player dump.
    """
    try:
        players = sleeper.all_players()
    except Exception:
        players = {}
    try:
        byes = schedule.bye_weeks(season)
    except Exception:
        byes = {}

    for r in rows:
        meta = players.get(r.get("player_id")) or {}
        if r.get("team"):
            r["bye"] = byes.get(r["team"])
        if not r.get("injury_status") and meta.get("injury_status"):
            r["injury_status"] = meta["injury_status"]
        if meta.get("depth_chart_order") is not None:
            r["depth_chart_order"] = meta["depth_chart_order"]
        # Age drives the aging curve, which bites RBs earliest and hardest.
        for field in ("age", "years_exp"):
            if meta.get(field) is not None:
                r[field] = meta[field]


def league_shape(lg: dict) -> tuple[list[str], int]:
    """The league's roster shape and team count, straight from the API.

    Deliberately has no default. A fallback team count would quietly price a
    league against a board it does not have - replacement levels, tiers,
    boom/bust bars and keeper costs all key off these two values, so a wrong
    guess is worse than a clear failure.
    """
    slots = lg.get("roster_positions") or []
    teams = lg.get("total_rosters")
    if not slots or not teams:
        raise ConfigMissing(
            "This league did not return a roster shape or team count from "
            "Sleeper, so nothing can be scored against it. Check the league ID."
        )
    return slots, int(teams)


def index_by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["player_id"]: r for r in rows if r.get("player_id")}


def find_player(name: str, rows: list[dict]) -> dict | None:
    """Fuzzy-match a player by name. Handles 'Ja'Marr', 'CMC'-style typos poorly
    but ordinary misspellings well."""
    target = name.strip().lower()
    by_name = {r["name"].lower(): r for r in rows}

    if target in by_name:
        return by_name[target]

    # Substring match (last-name-only queries).
    subs = [r for r in rows if target in r["name"].lower()]
    if len(subs) == 1:
        return subs[0]
    if subs:
        return max(subs, key=lambda r: r.get("points", 0))

    close = difflib.get_close_matches(target, list(by_name), n=1, cutoff=0.75)
    return by_name[close[0]] if close else None


def rostered_player_ids(league_id: str) -> set[str]:
    """Every player_id currently on any roster in the league."""
    owned: set[str] = set()
    for r in sleeper.league_rosters(league_id):
        for pid in (r.get("players") or []):
            owned.add(pid)
    return owned


def drafted_player_ids(draft_id: str) -> set[str]:
    return {p["player_id"] for p in sleeper.draft_picks(draft_id) if p.get("player_id")}


def team_name(league_id: str, roster_id: int) -> str:
    users = {u["user_id"]: u for u in sleeper.league_users(league_id)}
    for r in sleeper.league_rosters(league_id):
        if r.get("roster_id") == roster_id:
            u = users.get(r.get("owner_id")) or {}
            meta = u.get("metadata") or {}
            return meta.get("team_name") or u.get("display_name") or f"Roster {roster_id}"
    return f"Roster {roster_id}"
