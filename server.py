"""Sleeper Fantasy Football advisor - MCP server.

Read-only. Uses Sleeper's public API (no auth, no API key) and scores every
projection through your league's own scoring settings.

Run:  python server.py
"""

from __future__ import annotations

import functools

from mcp.server import MCPServer

from ff import (
    analysis,
    availability,
    construction,
    context,
    draftplan,
    handcuffs,
    keepers,
    montecarlo,
    news,
    schedule,
    scoring,
    sleeper,
    sos,
    streaming,
    tiers,
    usage,
    vegas,
    volatility,
    watch,
    weather,
    xfp,
)
from ff import (
    lineup as lineup_ctx,
)

# The NFL moved to a 17-game season in 2021, so durability history starts
# there. The end of the range is derived from the league being queried
# rather than pinned, so this does not go stale or assume a season.
FIRST_17_GAME_SEASON = 2021


def availability_seasons(lg: dict) -> list[str]:
    """Completed seasons to measure availability over, for this league."""
    season = int(lg.get("season") or sleeper.nfl_state()["season"])
    return [str(y) for y in range(FIRST_17_GAME_SEASON, season)]

# Sleeper does not document its settings enums. This ordering matches the order
# the options appear in Sleeper's own league setup UI, and is corroborated by
# `faab_suggestions` only being enabled on type-2 leagues.
WAIVER_TYPES = {0: "rolling", 1: "reverse standings", 2: "FAAB"}

mcp = MCPServer(
    name="sleeper-ff",
    version="0.1.0",
    instructions=(
        "Fantasy football advisor for Sleeper leagues. All projections are scored "
        "using the league's actual scoring_settings, and value is expressed as VOR "
        "(points over the last startable player at that position, given the league's "
        "roster shape). When giving advice, lead with the recommendation, then the "
        "reasoning. Prefer concrete numbers over vibes."
    ),
)


def _checked(fn):
    """Verify league settings on every call, and attach an alert if they moved.

    Wrapping at the tool boundary means the check runs for every command
    without each tool having to remember to ask for it. The check is fail-safe
    by construction: any error verifying settings is swallowed, because a
    watchdog that can break the thing it watches is worse than no watchdog.
    """

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        result = fn(*args, **kwargs)
        try:
            alert = watch.check()
        except Exception:
            return result
        if alert and isinstance(result, dict):
            # Only advance the baseline once the alert can actually be seen -
            # otherwise a change detected during a list-returning command would
            # be silently accepted and never reported.
            result["settings_alert"] = alert
            try:
                watch.check(acknowledge=True)
            except Exception:
                pass
        return result

    return inner


def tool(*d_args, **d_kwargs):
    """mcp.tool(), with the settings watchdog wrapped around it."""
    register = mcp.tool(*d_args, **d_kwargs)

    def decorate(fn):
        return register(_checked(fn))

    return decorate


# --- league basics --------------------------------------------------------


@tool()
def list_leagues() -> dict:
    """List the configured leagues and the current NFL week/season state."""
    cfg = context.load_config()
    state = sleeper.nfl_state()
    out = []
    for lg in cfg.get("leagues", []):
        try:
            info = sleeper.league(lg["league_id"])
            out.append(
                {
                    "alias": lg.get("name"),
                    "league_id": lg["league_id"],
                    "sleeper_name": info.get("name"),
                    "season": info.get("season"),
                    "status": info.get("status"),
                    "teams": info.get("total_rosters"),
                }
            )
        except Exception as e:  # a bad id in config shouldn't kill the tool
            out.append({"alias": lg.get("name"), "league_id": lg["league_id"], "error": str(e)})
    return {
        "nfl_state": {
            "season": state.get("season"),
            "week": state.get("week"),
            "season_type": state.get("season_type"),
        },
        "leagues": out,
        "configured_username": cfg.get("username"),
    }


@tool()
def league_info(league_id: str | None = None) -> dict:
    """Settings for a league: roster shape, scoring, waivers, playoffs.

    Use this first when you need to reason about league context (PPR or not,
    how many flex spots, FAAB budget, playoff weeks).
    """
    lid = context.resolve_league_id(league_id)
    lg = sleeper.league(lid)
    s = lg.get("settings") or {}
    sc = lg.get("scoring_settings") or {}
    roster = lg.get("roster_positions") or []

    starters = [p for p in roster if p not in scoring.NON_PLAYER_SLOTS]
    return {
        "name": lg.get("name"),
        "league_id": lid,
        "season": lg.get("season"),
        "status": lg.get("status"),
        "teams": lg.get("total_rosters"),
        "draft_id": lg.get("draft_id"),
        "starting_lineup": starters,
        "bench_spots": roster.count("BN"),
        "ir_spots": roster.count("IR") or s.get("reserve_slots", 0),
        "scoring_highlights": {
            "ppr": sc.get("rec"),
            "pass_td": sc.get("pass_td"),
            "te_premium": sc.get("bonus_rec_te"),
            "int": sc.get("pass_int"),
            "fumble_lost": sc.get("fum_lost"),
        },
        "waiver_type": WAIVER_TYPES.get(s.get("waiver_type"), s.get("waiver_type")),
        "uses_faab": s.get("waiver_type") == 2,
        # waiver_budget carries a default 100 even in leagues that don't use
        # FAAB, so only report it when the league is actually bidding.
        "faab_budget": s.get("waiver_budget") if s.get("waiver_type") == 2 else None,
        "trade_deadline_week": s.get("trade_deadline"),
        "playoff_teams": s.get("playoff_teams"),
        "playoff_week_start": s.get("playoff_week_start"),
        # Reported as configured-vs-in-use: a league can carry a non-zero
        # max_keepers it never actually uses, and advising on a mechanic
        # nobody plays with is worse than staying quiet.
        "max_keepers": s.get("max_keepers") or None,
        "keepers_in_use": keepers.keepers_in_use(lid)
        if (s.get("max_keepers") or 0) > 0
        else False,
    }


@tool()
def standings(league_id: str | None = None) -> list[dict]:
    """Current standings with record, points for/against, and FAAB left."""
    lid = context.resolve_league_id(league_id)
    users = {u["user_id"]: u for u in sleeper.league_users(lid)}
    rosters = sleeper.league_rosters(lid)
    budget = (sleeper.league(lid).get("settings") or {}).get("waiver_budget")

    rows = []
    for r in rosters:
        s = r.get("settings") or {}
        u = users.get(r.get("owner_id")) or {}
        meta = u.get("metadata") or {}
        pf = float(s.get("fpts", 0)) + float(s.get("fpts_decimal", 0)) / 100
        pa = float(s.get("fpts_against", 0)) + float(s.get("fpts_against_decimal", 0)) / 100
        rows.append(
            {
                "team": meta.get("team_name") or u.get("display_name") or f"Roster {r['roster_id']}",
                "manager": u.get("display_name"),
                "roster_id": r.get("roster_id"),
                "wins": s.get("wins", 0),
                "losses": s.get("losses", 0),
                "ties": s.get("ties", 0),
                "points_for": round(pf, 2),
                "points_against": round(pa, 2),
                "faab_remaining": (
                    budget - s.get("waiver_budget_used", 0) if budget is not None else None
                ),
            }
        )

    rows.sort(key=lambda x: (x["wins"], x["points_for"]), reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


@tool()
def my_roster(league_id: str | None = None, week: int | None = None) -> dict:
    """Your roster with league-scored projections, positional needs, and depth.

    Requires `username` in config.json.
    """
    lid = context.resolve_league_id(league_id)
    rid = context.my_roster_id(lid)
    if rid is None:
        return {
            "error": "Could not identify your roster. Set 'username' in config.json "
            "to your Sleeper display name."
        }
    return roster(rid, lid, week)


@tool()
def roster(roster_id: int, league_id: str | None = None, week: int | None = None) -> dict:
    """Any team's roster, scored for this league. Use for scouting trade partners."""
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid, week)
    idx = context.index_by_id(rows)

    target = next(
        (r for r in sleeper.league_rosters(lid) if r.get("roster_id") == roster_id), None
    )
    if not target:
        return {"error": f"No roster {roster_id} in this league."}

    players = [idx[pid] for pid in (target.get("players") or []) if pid in idx]
    players.sort(key=lambda p: p["vor"], reverse=True)

    needs = analysis.roster_needs(players, *context.league_shape(lg))
    best = analysis.optimal_lineup(players, lg.get("roster_positions") or [])

    return {
        "team": context.team_name(lid, roster_id),
        "roster_id": roster_id,
        "player_count": len(target.get("players") or []),
        "scored_players": len(players),
        "total_vor": round(sum(p["vor"] for p in players), 1),
        "needs": needs,
        "bye_conflicts": schedule.bye_conflicts(players),
        "optimal_lineup": best["lineup"],
        "projected_starter_total": best["projected_total"],
        "players": players,
    }


# --- draft ----------------------------------------------------------------


@tool()
def draft_board(
    league_id: str | None = None,
    position: str | None = None,
    limit: int = 25,
) -> dict:
    """Best available players in the draft, ranked by league-scored VOR.

    Shows ADP alongside value so you can spot who the room is undervaluing.
    Filter with `position` (QB/RB/WR/TE/DEF).
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)

    taken = set()
    draft_id = lg.get("draft_id")
    if draft_id:
        try:
            taken = context.drafted_player_ids(draft_id)
        except Exception:
            taken = set()
    taken |= context.rostered_player_ids(lid)

    avail = [r for r in rows if r["player_id"] not in taken]
    if position:
        avail = [r for r in avail if r["position"] == position.upper()]

    return {
        "league": lg.get("name"),
        "draft_status": (sleeper.draft(draft_id).get("status") if draft_id else None),
        "picks_made": len(taken),
        "scoring_note": "points are this league's scoring, not generic PPR",
        "available": avail[:limit],
    }


@tool()
def who_should_i_draft(
    league_id: str | None = None,
    pick_number: int | None = None,
    limit: int = 10,
) -> dict:
    """The core draft tool: who to take right now, and why.

    Blends value over replacement, your current positional needs, and whether
    a player has fallen past his ADP. Call this every time you're on the clock.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    idx = context.index_by_id(rows)

    draft_id = lg.get("draft_id")
    taken: set[str] = set()
    picks: list[dict] = []
    if draft_id:
        try:
            picks = sleeper.draft_picks(draft_id)
            taken = {p["player_id"] for p in picks if p.get("player_id")}
        except Exception:
            pass
    taken |= context.rostered_player_ids(lid)

    rid = context.my_roster_id(lid)
    owned: list[dict] = []
    if rid is not None:
        owned = [
            idx[p["player_id"]]
            for p in picks
            if p.get("roster_id") == rid and p.get("player_id") in idx
        ]
        if not owned:
            target = next(
                (r for r in sleeper.league_rosters(lid) if r.get("roster_id") == rid), None
            )
            if target:
                owned = [idx[pid] for pid in (target.get("players") or []) if pid in idx]

    if pick_number is None and picks:
        pick_number = len(picks) + 1

    avail = [r for r in rows if r["player_id"] not in taken]
    recs = analysis.draft_recommendations(
        avail, owned, *context.league_shape(lg),
        pick_number, limit,
    )

    avail_ids = {r["player_id"] for r in avail}
    warnings = [
        w
        for pos in ("RB", "WR", "TE", "QB")
        if (w := tiers.tier_break_warning(rows, pos, avail_ids))
    ]

    return {
        "league": lg.get("name"),
        "pick_number": pick_number,
        "tier_break_warnings": warnings,
        "your_roster_so_far": [
            {"name": p["name"], "pos": p["position"], "points": p["points"]} for p in owned
        ],
        "needs": analysis.roster_needs(
            owned, *context.league_shape(lg)
        ),
        "recommendations": recs,
    }


@tool()
def draft_results(league_id: str | None = None, limit: int = 50) -> dict:
    """Picks made so far, most recent first. Useful for tracking runs on a position."""
    lid = context.resolve_league_id(league_id)
    lg = sleeper.league(lid)
    draft_id = lg.get("draft_id")
    if not draft_id:
        return {"error": "This league has no draft."}

    d = sleeper.draft(draft_id)
    picks = sleeper.draft_picks(draft_id)
    _, rows = context.league_values(lid)
    idx = context.index_by_id(rows)

    out = []
    for p in reversed(picks[-limit:]):
        val = idx.get(p.get("player_id")) or {}
        meta = p.get("metadata") or {}
        out.append(
            {
                "pick": p.get("pick_no"),
                "round": p.get("round"),
                "roster_id": p.get("roster_id"),
                "player": f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip(),
                "position": meta.get("position"),
                "adp": val.get("adp"),
                "value_vs_adp": (
                    round(val["adp"] - p["pick_no"], 1)
                    if val.get("adp") and p.get("pick_no")
                    else None
                ),
            }
        )

    return {
        "draft_status": d.get("status"),
        "type": d.get("type"),
        "rounds": (d.get("settings") or {}).get("rounds"),
        "total_picks": len(picks),
        "recent_picks": out,
    }


# --- in-season ------------------------------------------------------------


@tool()
def waiver_targets(
    league_id: str | None = None,
    position: str | None = None,
    limit: int = 15,
) -> dict:
    """Best available free agents, with a suggested FAAB bid for each.

    Cross-references Sleeper's trending-adds so you know who the market is
    already moving on.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    owned = context.rostered_player_ids(lid)

    trending_ids = {}
    try:
        trending_ids = {t["player_id"]: t.get("count", 0) for t in sleeper.trending("add", 24, 50)}
    except Exception:
        pass

    rid = context.my_roster_id(lid)
    my_players: list[dict] = []
    budget = (lg.get("settings") or {}).get("waiver_budget", 100)
    if rid is not None:
        target = next(
            (r for r in sleeper.league_rosters(lid) if r.get("roster_id") == rid), None
        )
        if target:
            idx = context.index_by_id(rows)
            my_players = [idx[p] for p in (target.get("players") or []) if p in idx]
            budget = budget - (target.get("settings") or {}).get("waiver_budget_used", 0)

    needs = analysis.roster_needs(
        my_players, *context.league_shape(lg)
    )

    avail = [r for r in rows if r["player_id"] not in owned]
    if position:
        avail = [r for r in avail if r["position"] == position.upper()]

    uses_faab = (lg.get("settings") or {}).get("waiver_type") == 2

    # What rivals can still spend decides what you actually need to bid.
    rival_budgets: list[int] = []
    if uses_faab:
        full = (lg.get("settings") or {}).get("waiver_budget", 100)
        for r in sleeper.league_rosters(lid):
            if r.get("roster_id") == rid:
                continue
            rival_budgets.append(
                full - (r.get("settings") or {}).get("waiver_budget_used", 0)
            )

    week = sleeper.nfl_state().get("week")

    out = []
    for p in avail[:limit]:
        row = {**p, "trending_adds_24h": trending_ids.get(p["player_id"])}
        if uses_faab:
            row.update(
                analysis.faab_bid(p, budget, needs, week=week, rival_budgets=rival_budgets)
            )
        out.append(row)

    return {
        "waiver_type": WAIVER_TYPES.get((lg.get("settings") or {}).get("waiver_type")),
        "faab_remaining": budget if uses_faab else None,
        "note": (
            None
            if uses_faab
            else "This league uses waiver priority, not FAAB — spend your claim on "
            "the best player, there's no bidding."
        ),
        "targets": out,
    }


@tool()
def evaluate_trade(
    give: list[str],
    get: list[str],
    league_id: str | None = None,
) -> dict:
    """Is this trade good? Pass player names you'd give up and receive.

    Example: give=["<player on your roster>"], get=["<player you want>"]
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)

    # The deadline changes what a trade is for. Before it, you are buying
    # production for the rest of the season; once it passes, whatever you hold
    # is what you take into the playoffs, so depth stops being tradeable.
    deadline = (lg.get("settings") or {}).get("trade_deadline")
    week = sleeper.nfl_state().get("week")
    deadline_note = None
    if deadline and week:
        left = deadline - week
        if left < 0:
            deadline_note = (
                f"The trade deadline (week {deadline}) has passed - this trade "
                "cannot be made."
            )
        elif left == 0:
            deadline_note = f"Deadline is THIS week (week {deadline}) - last chance."
        elif left <= 2:
            deadline_note = (
                f"Only {left} week(s) until the week {deadline} deadline. If you "
                "need depth for the playoff run, buy it now."
            )

    give_p, get_p, missing = [], [], []
    for n in give:
        p = context.find_player(n, rows)
        give_p.append(p) if p else missing.append(n)
    for n in get:
        p = context.find_player(n, rows)
        get_p.append(p) if p else missing.append(n)

    if missing:
        return {"error": f"Couldn't find these players: {missing}"}

    rid = context.my_roster_id(lid)
    owned: list[dict] = []
    if rid is not None:
        target = next(
            (r for r in sleeper.league_rosters(lid) if r.get("roster_id") == rid), None
        )
        if target:
            idx = context.index_by_id(rows)
            owned = [idx[p] for p in (target.get("players") or []) if p in idx]

    verdict = analysis.evaluate_trade(
        give_p, get_p, owned, *context.league_shape(lg)
    )
    if deadline_note:
        verdict["deadline_note"] = deadline_note
    return verdict


@tool()
def set_lineup(
    league_id: str | None = None,
    week: int | None = None,
    signals: list[dict] | None = None,
) -> dict:
    """Your optimal starting lineup for a week, using this week's projections.

    The lineup is chosen on projected points alone, deliberately. Context is
    attached as flags instead - injury, weather, implied team total, boom/bust
    profile - and paired with close calls, the slots where a bench player is
    near enough that the flags should decide it rather than the projection.

    Pass `signals` (from news_signals -> submit_news_signals) to fold news into
    the flags. Those are model-read from prose rather than measured, so they
    are marked unverified and never move the lineup on their own.
    """
    lid = context.resolve_league_id(league_id)
    if week is None:
        week = sleeper.nfl_state().get("week", 1)

    rid = context.my_roster_id(lid)
    if rid is None:
        return {"error": "Set 'username' in config.json first."}

    lg, rows = context.league_values(lid, week)
    idx = context.index_by_id(rows)
    target = next((r for r in sleeper.league_rosters(lid) if r.get("roster_id") == rid), None)
    players = [idx[p] for p in (target.get("players") or []) if p in idx]

    result = analysis.optimal_lineup(players, lg.get("roster_positions") or [])
    season = lg.get("season") or sleeper.nfl_state()["season"]

    # Context layers. Each is optional and each failure is swallowed - a
    # missing weather key or an unposted betting line must never stop the
    # optimiser from returning a lineup.
    weather_by_team: dict = {}
    try:
        wx = weather.week_weather(season, week)
        for g in wx.get("games_with_forecast") or []:
            for team in (g.get("matchup") or "").split(" @ "):
                if team:
                    weather_by_team[team.strip()] = g
    except Exception:
        pass

    env_by_team: dict = {}
    try:
        for g in vegas.week_odds(season, week):
            for team in (g["home"], g["away"]):
                env_by_team[team] = vegas.team_environment(team, season, week)
    except Exception:
        pass

    vol: dict = {}
    try:
        vol = volatility.volatility_table(
            str(int(season) - 1), lg.get("scoring_settings"), _startable(lg)
        )
    except Exception:
        pass

    # News signals are supplied by the caller (they come from the keyless
    # extraction path), then re-validated here so an unchecked list cannot
    # reach the lineup.
    signals_by_id: dict = {}
    if signals:
        try:
            checked = news.validate_signals(signals, rows, context.find_player)
            for sig in checked["signals"]:
                signals_by_id.setdefault(sig["player_id"], []).append(sig)
        except Exception:
            signals_by_id = {}

    annotated = []
    for entry in result["lineup"]:
        flags = lineup_ctx.flags_for(
            entry, weather_by_team, env_by_team, vol, signals_by_id
        )
        for f in flags:
            f["_player"] = entry.get("name")
        annotated.append({**entry, "flags": flags or None})

    calls = lineup_ctx.close_calls(
        result["lineup"], result["bench"], weather_by_team, env_by_team, vol,
        signals_by_id=signals_by_id,
    )

    return {
        "week": week,
        "lineup": annotated,
        "projected_total": result["projected_total"],
        "read_this_first": lineup_ctx.summarise(
            [{"flags": a["flags"] or []} for a in annotated], calls
        ),
        "close_calls": calls or None,
        "bench": result["bench"][:10],
        "note": (
            "The lineup is chosen on projected points alone, deliberately - "
            "flags are context for you to apply, not silent adjustments. They "
            "matter most in close_calls, where the projections are too close "
            "to separate the options."
        ),
    }


@tool()
def matchup_preview(league_id: str | None = None, week: int | None = None) -> dict:
    """Preview your matchup for a week: both lineups and a projected margin."""
    lid = context.resolve_league_id(league_id)
    if week is None:
        week = sleeper.nfl_state().get("week", 1)

    rid = context.my_roster_id(lid)
    if rid is None:
        return {"error": "Set 'username' in config.json first."}

    ms = sleeper.matchups(lid, week)
    mine = next((m for m in ms if m.get("roster_id") == rid), None)
    if not mine:
        return {"error": f"No matchup found for week {week} (season may not have started)."}

    opp = next(
        (m for m in ms if m.get("matchup_id") == mine.get("matchup_id") and m.get("roster_id") != rid),
        None,
    )

    lg, rows = context.league_values(lid, week)
    idx = context.index_by_id(rows)

    def side(m: dict | None) -> dict:
        if not m:
            return {}
        players = [idx[p] for p in (m.get("players") or []) if p in idx]
        best = analysis.optimal_lineup(players, lg.get("roster_positions") or [])
        return {
            "team": context.team_name(lid, m["roster_id"]),
            "projected": best["projected_total"],
            "lineup": best["lineup"],
        }

    me, them = side(mine), side(opp)
    margin = round(me.get("projected", 0) - them.get("projected", 0), 1) if them else None
    return {
        "week": week,
        "you": me,
        "opponent": them,
        "projected_margin": margin,
        "read": (
            "favored" if margin and margin > 8
            else "underdog" if margin and margin < -8
            else "coin flip"
        ) if margin is not None else None,
    }


@tool()
def player(name: str, league_id: str | None = None, week: int | None = None) -> dict:
    """Look up one player: league-scored projection, VOR, ADP, rank, owner."""
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid, week)
    p = context.find_player(name, rows)
    if not p:
        return {"error": f"No player matching '{name}'."}

    owner = None
    for r in sleeper.league_rosters(lid):
        if p["player_id"] in (r.get("players") or []):
            owner = context.team_name(lid, r["roster_id"])
            break

    season = lg.get("season") or sleeper.nfl_state()["season"]
    prior = str(int(season) - 1)

    # Usage answers "is the role real?" — the projection alone can't.
    try:
        u = usage.usage_table(prior).get(p["player_id"])
    except Exception:
        u = None

    try:
        matchups = sos.player_sos(
            p.get("team"), p["position"], season, prior, lg.get("scoring_settings")
        )
    except Exception:
        matchups = None

    # Two players with the same projection can be very different assets.
    try:
        vol = volatility.volatility_table(
            prior, lg.get("scoring_settings"), _startable(lg)
        ).get(p["player_id"])
    except Exception:
        vol = None

    # Opportunity-only expectation: separates a real role from a lucky one.
    try:
        xfp_rec = xfp.xfp_table(prior, lg.get("scoring_settings")).get(p["player_id"])
    except Exception:
        xfp_rec = None

    try:
        avail_table = availability.availability_table(availability_seasons(lg))
        avail = avail_table.get(p["player_id"])
        base_rates = availability.position_base_rates(avail_table)
    except Exception:
        avail, base_rates = None, None

    try:
        tier = next(
            (
                t["tier"]
                for t in tiers.assign_tiers(rows, p["position"])
                if any(x["player_id"] == p["player_id"] for x in t["players"])
            ),
            None,
        )
    except Exception:
        tier = None

    return {
        **p,
        "owner": owner or "free agent",
        "week": week or "full season",
        "positional_tier": tier,
        "prior_season_usage": u,
        "usage_read": usage.opportunity_flags(u, p["position"]),
        "schedule": matchups,
        "game_environment": _game_env(p.get("team"), season, week),
        "expected_points": xfp_rec,
        "regression_read": xfp.regression_read(xfp_rec),
        "consistency": vol,
        "consistency_read": volatility.consistency_read(vol, p["position"]),
        "format_fit": volatility.format_fit(vol),
        "availability": avail,
        "availability_read": availability.availability_read(
            avail, base_rates, p.get("age")
        ),
    }


@tool()
def position_tiers(
    position: str,
    league_id: str | None = None,
    available_only: bool = False,
    max_tiers: int = 6,
) -> dict:
    """Tier a position by where the real value cliffs fall.

    Use this to decide whether you can wait a round. Several players left in
    the current tier means you can; one or two left means the cliff is next.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    pos = position.upper()

    pool = rows
    if available_only:
        taken = context.rostered_player_ids(lid)
        if lg.get("draft_id"):
            try:
                taken |= context.drafted_player_ids(lg["draft_id"])
            except Exception:
                pass
        pool = [r for r in rows if r["player_id"] not in taken]

    grouped = tiers.assign_tiers(pool, pos)[:max_tiers]
    return {
        "league": lg.get("name"),
        "position": pos,
        "available_only": available_only,
        "tiers": [
            {
                "tier": t["tier"],
                "count": t["count"],
                "vor_range": t["value_range"],
                "players": [
                    {
                        "name": x["name"],
                        "team": x.get("team"),
                        "points": x["points"],
                        "vor": x["vor"],
                        "adp": x.get("adp"),
                    }
                    for x in t["players"]
                ],
            }
            for t in grouped
        ],
    }


@tool()
def game_weather(week: int | None = None, season: str | None = None) -> dict:
    """Kickoff weather and its fantasy impact for a week's games.

    Only useful inside the ~5-day forecast window, so this is a lineup tool,
    not a draft tool. Dome and retractable-roof games are separated out rather
    than reported with a meaningless forecast.
    """
    state = sleeper.nfl_state()
    return weather.week_weather(
        season or state["season"], week or state.get("week") or 1
    )


def _startable(lg: dict) -> dict[str, float] | None:
    """League-wide startable counts, from this league's own roster shape."""
    slots = lg.get("roster_positions") or []
    teams = lg.get("total_rosters")
    if not slots or not teams:
        return None
    return scoring.starter_counts(slots, teams)


def _game_env(team: str | None, season: str, week: int | None) -> dict | None:
    """Vegas context for a player's next game, when a line exists."""
    if not team:
        return None
    try:
        state = sleeper.nfl_state()
        return vegas.team_environment(team, season, week or state.get("week") or 1)
    except Exception:
        return None


@tool()
def consistency_report(
    league_id: str | None = None,
    position: str | None = None,
    limit: int = 15,
    sort_by: str = "ceiling",
    min_projected: float = 120.0,
) -> dict:
    """Floor, ceiling and boom/bust rates from last season's weekly scores.

    sort_by = ceiling | floor | steadiest | most_volatile. Use floor for weekly
    head-to-head, ceiling for best-ball.

    min_projected filters to players worth rostering. It matters most for the
    volatility sorts: coefficient of variation explodes when the mean is near
    zero, so without it the list fills with deep reserves who scored twice all
    season rather than the high-variance starters you actually want.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    prior = str(int(lg.get("season") or sleeper.nfl_state()["season"]) - 1)
    vol = volatility.volatility_table(
        prior, lg.get("scoring_settings"), _startable(lg)
    )

    merged = []
    for r in rows:
        v = vol.get(r["player_id"])
        if not v:
            continue
        if position and r["position"] != position.upper():
            continue
        if (r.get("points") or 0) < min_projected:
            continue
        merged.append({"name": r["name"], "position": r["position"], "team": r.get("team"), "projected": r["points"], **v})

    keys = {
        "ceiling": lambda x: -x["ceiling"],
        "floor": lambda x: -x["floor"],
        "steadiest": lambda x: (x["cv"] is None, x["cv"]),
        "most_volatile": lambda x: (x["cv"] is None, -(x["cv"] or 0)),
    }
    merged.sort(key=keys.get(sort_by, keys["ceiling"]))

    return {
        "league": lg.get("name"),
        "based_on_season": prior,
        "sorted_by": sort_by,
        "note": "Boom/bust lines are per-position percentiles of startable players.",
        "players": merged[:limit],
    }


@tool()
def durability_report(league_id: str | None = None, limit: int = 15) -> dict:
    """Availability history for the top of the board, worst first.

    Measures games played, which mixes injury with benchings and rest - so read
    it as availability rather than as a medical assessment.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    table = availability.availability_table(availability_seasons(lg))
    base_rates = availability.position_base_rates(table)

    merged = []
    for r in rows[: limit * 6]:
        rec = table.get(r["player_id"])
        if not rec:
            continue
        merged.append(
            {
                "name": r["name"],
                "position": r["position"],
                "age": r.get("age"),
                "vor": r.get("vor"),
                **{k: rec[k] for k in ("debut_season", "availability_pct", "missed_per_season", "games_by_season")},
                "read": availability.availability_read(rec, base_rates, r.get("age")),
            }
        )

    merged.sort(key=lambda x: x["availability_pct"])
    return {
        "league": lg.get("name"),
        "seasons": availability_seasons(lg),
        "position_base_rates": base_rates,
        "caveat": (
            "Games played conflates injury, benchings and rest. QB shows the "
            "highest missed-game rate largely because backups get benched, not hurt."
        ),
        "least_available": merged[:limit],
    }


@tool()
def handcuff_report(league_id: str | None = None) -> dict:
    """Which of your RBs need their backup rostered, and who that backup is.

    Priority is driven by how much volume the starter absorbs - a workhorse
    leaves a large transferable role behind him, a committee back does not.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    idx = context.index_by_id(rows)
    taken = context.rostered_player_ids(lid)

    rid = context.my_roster_id(lid)
    owned: list[dict] = []
    if rid is not None:
        target = next(
            (r for r in sleeper.league_rosters(lid) if r.get("roster_id") == rid), None
        )
        if target:
            owned = [idx[p] for p in (target.get("players") or []) if p in idx]

    # Usage and availability sharpen the priority call considerably.
    prior = str(int(lg.get("season") or sleeper.nfl_state()["season"]) - 1)
    try:
        u = usage.usage_table(prior)
        avail_tbl = availability.availability_table(availability_seasons(lg))
    except Exception:
        u, avail_tbl = {}, {}

    enriched = []
    for p in owned:
        enriched.append(
            {
                **p,
                "prior_usage": u.get(p["player_id"]),
                "availability_pct": (avail_tbl.get(p["player_id"]) or {}).get(
                    "availability_pct"
                ),
            }
        )

    report = handcuffs.roster_handcuffs(enriched, idx, taken)

    empty_note = None
    if not owned:
        empty_note = (
            "You have no players yet - this league has not drafted. Run this "
            "again once your roster exists."
        )
    elif not report:
        empty_note = "No handcuff-worthy players on your roster (no RBs)."

    return {
        "league": lg.get("name"),
        "status": empty_note,
        "handcuffs": report,
        "note": (
            "Handcuffing is an RB strategy - RB value is volume-driven and volume "
            "transfers to the next man up. Other positions do not behave this way."
        ),
    }


@tool()
def streaming_options(
    position: str = "DEF",
    week: int | None = None,
    league_id: str | None = None,
    available_only: bool = True,
    limit: int = 10,
) -> dict:
    """Best streaming plays for a week at DEF, K, QB or TE.

    The signal inverts by position: a defense wants its OPPONENT implied low,
    while an offensive player wants his OWN team implied high and a soft
    positional matchup.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    season = lg.get("season") or sleeper.nfl_state()["season"]
    prior = str(int(season) - 1)
    wk = week or sleeper.nfl_state().get("week") or 1
    pos = position.upper()

    slots = lg.get("roster_positions") or []
    if pos not in slots:
        return {
            "league": lg.get("name"),
            "position": pos,
            "error": f"This league does not start a {pos}.",
        }

    avail = None
    if available_only:
        taken = context.rostered_player_ids(lid)
        avail = {r["player_id"] for r in rows if r["player_id"] not in taken}

    if pos == "DEF":
        ranked = streaming.defense_streamers(season, wk, rows, avail, limit)
    else:
        # Nobody streams an elite option; cap the pool at replacement-ish level.
        caps = {"QB": 260.0, "TE": 150.0, "K": 200.0}
        ranked = streaming.position_streamers(
            pos,
            season,
            prior,
            wk,
            rows,
            avail,
            limit,
            caps.get(pos),
            scoring=lg.get("scoring_settings"),
        )

    return {
        "league": lg.get("name"),
        "position": pos,
        "week": wk,
        "options": ranked,
        "note": streaming.streaming_note(pos, bool(ranked)),
    }


@tool()
def roster_risk(league_id: str | None = None, best_ball: bool = False) -> dict:
    """Correlation and concentration risk across your roster.

    Flags QB/pass-catcher stacks and over-exposure to a single NFL team. These
    are tiebreakers - never pass on a clearly better player to avoid them.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    idx = context.index_by_id(rows)

    rid = context.my_roster_id(lid)
    owned: list[dict] = []
    if rid is not None:
        target = next(
            (r for r in sleeper.league_rosters(lid) if r.get("roster_id") == rid), None
        )
        if target:
            owned = [idx[p] for p in (target.get("players") or []) if p in idx]

    if not owned:
        return {
            "league": lg.get("name"),
            "status": "No players yet - this league has not drafted.",
        }

    report = construction.correlation_report(owned, best_ball)
    report.update(
        {
            "league": lg.get("name"),
            "bye_conflicts": schedule.bye_conflicts(owned),
        }
    )
    return report


@tool()
def ir_stash_targets(league_id: str | None = None, limit: int = 10) -> dict:
    """Injured players worth stashing, given this league's IR slots."""
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    taken = context.rostered_player_ids(lid)
    reserve = (lg.get("settings") or {}).get("reserve_slots") or 0

    avail = [r for r in rows if r["player_id"] not in taken]
    return {
        "league": lg.get("name"),
        **construction.stash_candidates(avail, reserve, limit),
    }


@tool()
def waiver_strategy(league_id: str | None = None) -> dict:
    """How to spend your waiver claim, for FAAB or priority leagues alike."""
    lid = context.resolve_league_id(league_id)
    lg = sleeper.league(lid)
    settings = lg.get("settings") or {}
    wtype = settings.get("waiver_type")
    week = sleeper.nfl_state().get("week")
    total = context.league_shape(lg)[1]

    if wtype == 2:
        return {
            "league": lg.get("name"),
            "system": "FAAB",
            "budget": settings.get("waiver_budget"),
            "note": (
                "Use waiver_targets for per-player bid ranges. Bids scale with "
                "season timing and with rivals' remaining budgets."
            ),
        }

    rid = context.my_roster_id(lid)
    order = None
    for r in sleeper.league_rosters(lid):
        if r.get("roster_id") == rid:
            order = (r.get("settings") or {}).get("waiver_position")
            break

    return {
        "league": lg.get("name"),
        "system": WAIVER_TYPES.get(wtype, "unknown"),
        **construction.waiver_priority_advice(order, total, week),
    }


@tool()
def simulate_season(
    names: str,
    league_id: str | None = None,
    runs: int = 4000,
) -> dict:
    """Monte Carlo a player's season, or compare several. Comma-separate names.

    Converts a point projection into a distribution by sampling games played
    from his availability history and weekly scores from his volatility, so you
    see floor, median and ceiling rather than a single number.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    season = lg.get("season") or sleeper.nfl_state()["season"]
    prior = str(int(season) - 1)

    try:
        vol = volatility.volatility_table(
        prior, lg.get("scoring_settings"), _startable(lg)
    )
        avail_tbl = availability.availability_table(availability_seasons(lg))
    except Exception:
        vol, avail_tbl = {}, {}

    results, labels, missing = [], [], []
    for raw in [n.strip() for n in names.split(",") if n.strip()]:
        pl = context.find_player(raw, rows)
        if not pl:
            missing.append(raw)
            continue
        v = vol.get(pl["player_id"]) or {}
        a = avail_tbl.get(pl["player_id"]) or {}
        sim = montecarlo.simulate_player(
            pl["points"],
            pl["position"],
            cv=v.get("cv"),
            availability_pct=a.get("availability_pct"),
            runs=runs,
            seed=42,  # reproducible across calls
        )
        results.append(
            {
                "name": pl["name"],
                "position": pl["position"],
                "team": pl.get("team"),
                **sim,
                "read": montecarlo.outcome_read(sim, pl["position"]),
            }
        )
        labels.append(pl["name"])

    out = {
        "league": lg.get("name"),
        "not_found": missing or None,
        "players": results,
        "caveat": (
            "A model, not a forecast. It inherits every bias in the underlying "
            "projection and only adds the shape the projection omits."
        ),
    }
    if len(results) > 1:
        out["comparison"] = montecarlo.compare(
            [{k: r[k] for k in ("median", "p10", "p90")} for r in results], labels
        )
    return out


@tool()
def regression_candidates(
    league_id: str | None = None,
    position: str | None = None,
    direction: str = "both",
    min_opportunities: int = 100,
    limit: int = 10,
) -> dict:
    """Players whose results outran their opportunity, or fell short of it.

    Opportunity is sticky year to year; efficiency and touchdown luck are not.
    So a player far above his expected points is a sell/fade candidate, and one
    far below is a buy - the role was real even though the box score was not.

    direction = both | overperformers | underperformers
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    season = lg.get("season") or sleeper.nfl_state()["season"]
    prior = str(int(season) - 1)
    scoring_settings = lg.get("scoring_settings")

    try:
        table = xfp.xfp_table(prior, scoring_settings)
        weights = xfp.fit_weights(prior, scoring_settings)
    except Exception as e:
        return {"error": f"Could not build the xFP model: {e}"}

    idx = context.index_by_id(rows)
    merged = []
    for pid, rec in table.items():
        row = idx.get(pid)
        if not row:
            continue
        if position and row["position"] != position.upper():
            continue
        if rec["opportunities"] < min_opportunities:
            continue
        merged.append(
            {
                "name": row["name"],
                "position": row["position"],
                "team": row.get("team"),
                "projected_points": row.get("points"),
                "adp": row.get("adp"),
                **{
                    k: rec[k]
                    for k in (
                        "expected_points",
                        "actual_points",
                        "delta",
                        "efficiency_ratio",
                        "opportunities_per_game",
                    )
                },
                "read": xfp.regression_read(rec),
            }
        )

    merged.sort(key=lambda x: x["delta"], reverse=True)
    out = {
        "league": lg.get("name"),
        "based_on_season": prior,
        "points_per_opportunity": {
            pos: d["weights"] for pos, d in sorted(weights.items())
        },
        "note": (
            "Weights are fitted to this league's own scoring, constrained to be "
            "non-negative. Only true opportunity is used as input - targets, "
            "carries and their red-zone subsets."
        ),
    }
    if direction in ("both", "overperformers"):
        out["overperformers_regression_risk"] = merged[:limit]
    if direction in ("both", "underperformers"):
        out["underperformers_bounce_back"] = merged[-limit:][::-1]
    return out


@tool()
def draft_plan(
    league_id: str | None = None,
    slot: int | None = None,
    through_round: int = 6,
    limit: int = 6,
) -> dict:
    """Who will realistically be available at each of YOUR picks.

    The draft board ranks who is best; this ranks who you can actually have.
    Each player's draft position is modelled as a distribution around his ADP,
    so a name is only suggested at a pick he has a real chance of reaching.

    `slot` defaults to your position in the league's own draft order.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    teams = context.league_shape(lg)[1]

    draft_id = lg.get("draft_id")
    rounds = None
    draft_type = "snake"
    reversal_round = 0
    resolved_slot = slot
    if draft_id:
        try:
            d = sleeper.draft(draft_id)
            dsettings = d.get("settings") or {}
            rounds = dsettings.get("rounds")
            reversal_round = dsettings.get("reversal_round") or 0
            draft_type = (d.get("type") or "snake").lower()
            if resolved_slot is None:
                cfg = context.load_config()
                me = sleeper.user(cfg.get("username", ""))
                resolved_slot = (d.get("draft_order") or {}).get(me.get("user_id"))
        except Exception:
            pass

    if draft_type == "auction":
        return {
            "league": lg.get("name"),
            "draft_type": "auction",
            "error": (
                "This league runs an auction draft, which has no pick slots - "
                "there is no board position to plan around. Use draft_board for "
                "values instead."
            ),
        }

    if not rounds:
        return {
            "league": lg.get("name"),
            "error": (
                "The draft has not reported a round count yet, so picks cannot "
                "be computed. Try again once the draft is configured."
            ),
        }

    if resolved_slot is None:
        return {
            "league": lg.get("name"),
            "error": (
                "Draft order is not set yet and no slot was supplied. Pass "
                "slot=N to plan hypothetically."
            ),
        }

    taken = context.rostered_player_ids(lid)
    if draft_id:
        try:
            taken |= context.drafted_player_ids(draft_id)
        except Exception:
            pass

    avail = [r for r in rows if r["player_id"] not in taken]
    picks = draftplan.picks_for_slot(
        resolved_slot, teams, rounds, draft_type, reversal_round
    )

    return {
        "league": lg.get("name"),
        "slot": resolved_slot,
        "teams": teams,
        "rounds": rounds,
        "draft_type": draft_type,
        "reversal_round": reversal_round or None,
        "your_picks": picks[:through_round],
        "structure": draftplan.plan_notes(picks, teams),
        "rounds_detail": draftplan.plan(
            avail, resolved_slot, teams, rounds, through_round, limit,
            draft_type, reversal_round,
        ),
        "note": (
            "available_pct is P(still on the board at that pick), from a normal "
            "model around ADP whose spread widens deeper into the draft. "
            "expected_value = VOR x that probability."
        ),
    }


@tool()
def news_signals(
    league_id: str | None = None,
    limit: int = 20,
    min_confidence: str = "low",
    mode: str = "auto",
) -> dict:
    """Structured signals extracted from recent NFL news prose.

    mode = auto | delegate | direct. "delegate" needs no API key: the tool
    returns the article text and the extraction contract for you to read, and
    you pass your result to `submit_news_signals` for validation. "direct" has
    the server call Claude itself (needs a key). "auto" delegates unless a key
    is configured.

    This is the only model-backed tool here, and it is deliberately fenced: it
    reads text and reports what it says, never producing a projection, ranking
    or any other number. Signals naming a player who cannot be resolved against
    this league are discarded rather than shown.

    Treat the output as unverified context to weigh alongside the measured
    metrics - not as a replacement for them. Optional: requires the `anthropic`
    package and an API key, and reports plainly when either is missing.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)

    articles = news.fetch_headlines(limit)
    if not articles:
        return {"league": lg.get("name"), "status": "No news available.", "signals": []}

    ok, reason = news.available()

    # Delegated mode is the default, and it needs no API key: you are already a
    # language model, so read these yourself rather than having the server pay
    # for a second one. Whatever you extract goes through the same validation.
    if mode == "delegate" or (mode == "auto" and not ok):
        return {
            "league": lg.get("name"),
            "mode": "delegate",
            "why": (
                "No API key needed - extract the signals yourself from the text "
                "below, then call submit_news_signals to have them validated."
                if not ok
                else "Delegated extraction requested."
            ),
            **news.extraction_brief(articles),
        }

    if not ok:
        return {"league": lg.get("name"), "status": reason, "signals": []}

    extracted = news.extract_signals(articles)
    if extracted.get("error"):
        return {"league": lg.get("name"), "status": extracted["error"], "signals": []}

    checked = news.validate_signals(
        extracted["signals"], rows, context.find_player
    )

    rank = {"high": 0, "medium": 1, "low": 2}
    floor = rank.get(min_confidence.lower(), 2)
    kept = [
        s for s in checked["signals"] if rank.get(s.get("confidence"), 9) <= floor
    ]

    return {
        "league": lg.get("name"),
        "articles_read": len(articles),
        "signals": kept,
        "discarded": checked["discarded"] or None,
        "caveat": (
            "Model-extracted from news text and NOT verified against data. "
            "Every other number in this tool is computed deterministically; "
            "these are not. Weigh them as context, and confirm anything "
            "load-bearing before acting on it."
        ),
    }


@tool()
def submit_news_signals(
    signals: list[dict],
    league_id: str | None = None,
    min_confidence: str = "low",
) -> dict:
    """Validate signals you extracted from news text against this league.

    The second half of the keyless path. Pass the signals you read out of the
    articles returned by `news_signals`; each one is checked against the
    league's real player list and anything that does not resolve is discarded
    rather than reported. This is the same validation the API-backed path runs.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)

    checked = news.validate_signals(signals or [], rows, context.find_player)

    rank = {"high": 0, "medium": 1, "low": 2}
    floor = rank.get(min_confidence.lower(), 2)
    kept = [s for s in checked["signals"] if rank.get(s.get("confidence"), 9) <= floor]

    return {
        "league": lg.get("name"),
        "submitted": len(signals or []),
        "validated": len(kept),
        "signals": kept,
        "discarded": checked["discarded"] or None,
        "caveat": (
            "Extracted from news prose and NOT verified against data. Every "
            "other number in this tool is computed deterministically; these are "
            "not. Weigh them as context and confirm anything load-bearing."
        ),
    }


@tool()
def keeper_analysis(
    league_id: str | None = None,
    slot: int | None = None,
    cost_rule: str = "same_round",
    undrafted_cost_round: int | None = None,
) -> dict:
    """Which players are worth keeping, given the pick each one costs.

    A keeper is never free - keeping someone forfeits a draft pick, so the
    question is whether he beats whoever that pick would have returned. That
    difference is his surplus, and it is what this ranks on. An elite player at
    an expensive cost can be a bad keeper; a useful starter at a cheap cost is
    often a great one.

    cost_rule = same_round | round_earlier | two_rounds_earlier. Sleeper stores
    that keepers exist but not what they cost, so the league's own rule is a
    parameter rather than a guess.
    """
    lid = context.resolve_league_id(league_id)
    lg, rows = context.league_values(lid)
    settings = keepers.keeper_settings(lg)

    if not settings["configured"]:
        return {
            "league": lg.get("name"),
            "status": (
                "This league does not use keepers (max_keepers is 0), so there "
                "is nothing to evaluate."
            ),
            **settings,
        }

    idx = context.index_by_id(rows)
    rid = context.my_roster_id(lid)
    owned: list[dict] = []
    if rid is not None:
        target = next(
            (r for r in sleeper.league_rosters(lid) if r.get("roster_id") == rid), None
        )
        if target:
            owned = [idx[p] for p in (target.get("players") or []) if p in idx]

    if not owned:
        return {
            "league": lg.get("name"),
            **settings,
            "status": (
                "You have no players yet, so there is nothing to keep. Run this "
                "again once a roster exists."
            ),
        }

    prior, prior_note = keepers.prior_draft_rounds(lg)

    if slot is None and lg.get("draft_id"):
        try:
            d = sleeper.draft(lg["draft_id"])
            cfg = context.load_config()
            me = sleeper.user(cfg.get("username", ""))
            slot = (d.get("draft_order") or {}).get(me.get("user_id"))
        except Exception:
            pass

    analysed = keepers.analyse(
        owned,
        rows,
        lg,
        slot=slot,
        cost_rule=cost_rule,
        prior_rounds=prior,
        undrafted_cost_round=undrafted_cost_round,
        draft_rounds=keepers.draft_length(lg),
    )

    return {
        "league": lg.get("name"),
        **settings,
        "keepers_in_use": keepers.keepers_in_use(lid),
        "cost_rule": cost_rule,
        "cost_data": prior_note
        or f"Costs derived from last season's draft ({len(prior)} players).",
        "candidates": analysed,
        **keepers.recommend(analysed, settings["max_keepers"]),
    }


@tool()
def vegas_lines(week: int | None = None, season: str | None = None) -> dict:
    """Betting lines and the game scripts they imply, ranked by scoring environment.

    Implied team total (total/2 +/- spread/2) is the most useful single number
    here: it says how big a pie each offense is expected to share.
    """
    state = sleeper.nfl_state()
    szn = season or state["season"]
    wk = week or state.get("week") or 1
    games = vegas.week_odds(szn, wk)
    if not games:
        return {"season": szn, "week": wk, "error": "No lines posted for this week yet."}
    return {**vegas.best_and_worst(szn, wk), "games": games}


@tool()
def trending_players(kind: str = "add", hours: int = 24, limit: int = 20) -> list[dict]:
    """Who the whole Sleeper userbase is adding or dropping. kind = add | drop."""
    data = sleeper.trending(kind, hours, limit)
    players = sleeper.all_players()
    out = []
    for t in data:
        meta = players.get(t["player_id"]) or {}
        out.append(
            {
                "name": f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip(),
                "position": meta.get("position"),
                "team": meta.get("team"),
                "count": t.get("count"),
                "injury_status": meta.get("injury_status"),
            }
        )
    return out


@tool()
def refresh_data() -> dict:
    """Clear the local cache. Use after a trade, a waiver run, or mid-draft."""
    n = sleeper.clear_cache()
    return {"cleared_files": n, "note": "next call refetches from Sleeper"}


if __name__ == "__main__":
    mcp.run()


@tool()
def settings_check() -> dict:
    """Show each league's live scoring and roster settings, and any drift.

    The same verification runs automatically on every command; this exposes it
    directly so you can inspect the current fingerprint.
    """
    return {
        "leagues": watch.status(),
        "drift": watch.check(acknowledge=True) or "no changes since last check",
        "watching": (
            "scoring_settings, roster_positions, total_rosters, and the settings "
            "that affect value: waiver type/budget, trade deadline, playoff "
            "structure, IR and taxi slots."
        ),
    }
