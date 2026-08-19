"""Prior-season usage and opportunity metrics.

Projections tell you what a player is expected to do. Usage tells you *why* —
whether the role is real. Sleeper's stats endpoint carries the raw inputs
(snaps, targets, air yards, red-zone looks), so we derive the standard
opportunity metrics rather than trusting a projection blindly.

The guiding principle: volume beats efficiency. A mediocre back with 15 touches
outscores an efficient one with 6.
"""

from __future__ import annotations

from . import sleeper

POSITIONS = ["QB", "RB", "WR", "TE"]

# Dominator rating measures share of a team's *receiving* production, so it is
# only meaningful for players whose job is catching passes.
RECEIVING_POSITIONS = {"WR", "TE"}

# 17-game regular season, used to put share metrics on a per-game footing.
GAMES_PER_SEASON = 17


def _season_stats(season: str) -> list[dict]:
    qs = "&".join(f"position[]={p}" for p in POSITIONS)
    url = f"https://api.sleeper.app/stats/nfl/{season}?season_type=regular&{qs}&order_by=pts_ppr"
    return sleeper._get(url, f"stats_season_{season}", 24 * 3600)


def _share(
    player_total: float | None, team_total: float | None, games: float | None
) -> float | None:
    """A player's share of his team's volume, on a per-game footing.

    Season totals are the wrong denominator. Sleeper scopes `tm_off_snp` to the
    games a player actually dressed for, so snap share is already correct - but
    team target and carry totals are season-long, and dividing a four-game
    player's counting stats by them makes an injured alpha look like a decoy.
    A receiver who plays a quarter of the season reads as peripheral on season
    totals and as a clear alpha per game.

    `games` is passed explicitly rather than closed over: this used to be a
    nested function defined inside the per-player loop, which worked only
    because it was called in the same iteration it was defined in.
    """
    if not player_total or not team_total or not games:
        return None
    return (player_total / games) / (team_total / GAMES_PER_SEASON)


def _safe_div(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return a / b


def usage_table(season: str) -> dict[str, dict]:
    """Per-player opportunity metrics for a completed season.

    Returns {player_id: {...metrics...}}.
    """
    rows = _season_stats(season)

    # Team-level denominators, needed for share metrics.
    team_targets: dict[str, float] = {}
    team_carries: dict[str, float] = {}
    team_rz: dict[str, float] = {}
    team_air: dict[str, float] = {}
    team_rec_yd: dict[str, float] = {}
    team_rec_td: dict[str, float] = {}
    for r in rows:
        team = (r.get("player") or {}).get("team")
        s = r.get("stats") or {}
        if not team:
            continue
        team_targets[team] = team_targets.get(team, 0) + (s.get("rec_tgt") or 0)
        team_carries[team] = team_carries.get(team, 0) + (s.get("rush_att") or 0)
        team_rz[team] = team_rz.get(team, 0) + (s.get("rush_rz_att") or 0) + (
            s.get("rec_rz_tgt") or 0
        )
        team_air[team] = team_air.get(team, 0) + (s.get("rec_air_yd") or 0)
        team_rec_yd[team] = team_rec_yd.get(team, 0) + (s.get("rec_yd") or 0)
        team_rec_td[team] = team_rec_td.get(team, 0) + (s.get("rec_td") or 0)

    out: dict[str, dict] = {}
    for r in rows:
        s = r.get("stats") or {}
        meta = r.get("player") or {}
        team = meta.get("team")
        pid = r.get("player_id")
        if not pid:
            continue

        games = s.get("gp") or 0
        targets = s.get("rec_tgt") or 0
        carries = s.get("rush_att") or 0
        touches = targets + carries
        rz = (s.get("rush_rz_att") or 0) + (s.get("rec_rz_tgt") or 0)

        out[pid] = {
            "season": season,
            "games": games or None,
            "snap_share": _pct(_safe_div(s.get("off_snp"), s.get("tm_off_snp"))),
            "target_share": _pct(_share(targets, team_targets.get(team), games)),
            "carry_share": _pct(_share(carries, team_carries.get(team), games)),
            "rz_share": _pct(_share(rz, team_rz.get(team), games)),
            "targets": targets or None,
            "carries": carries or None,
            "touches_per_game": round(touches / games, 1) if games else None,
            # NOTE: Sleeper gives *completed* air yards, not intended air yards
            # on all targets (verified: rec_air_yd + rec_yar == rec_yd exactly).
            # So true aDOT is NOT derivable here. What we can honestly report is
            # how deep his catches come and how much he adds after them.
            "depth_per_catch": _round(_safe_div(s.get("rec_air_yd"), s.get("rec"))),
            "yac_per_catch": _round(_safe_div(s.get("rec_yar"), s.get("rec"))),
            "air_yards_share_of_yds": _pct(
                _safe_div(s.get("rec_air_yd"), s.get("rec_yd"))
            ),
            "yards_per_target": _round(s.get("rec_ypt")),
            "yards_per_carry": _round(s.get("rush_ypa")),
            "yac_per_carry": _round(_safe_div(s.get("rush_yac"), carries)),
            # --- composite opportunity/efficiency metrics -------------------
            # WOPR: 1.5 x target share + 0.7 x air-yards share. The standard
            # formula uses *intended* air yards on all targets; Sleeper only
            # publishes air yards on completions, so this is a close proxy
            # rather than the canonical number. Named accordingly.
            "wopr_proxy": _round(
                _wopr(
                    _share(targets, team_targets.get(team), games),
                    _share(s.get("rec_air_yd"), team_air.get(team), games),
                )
            ),
            # RACR: receiving yards per air yard. Above 1.0 means he gains more
            # than the depth of the throw, i.e. he creates after the catch.
            "racr": _round(_racr(s.get("rec_yd"), s.get("rec_air_yd"))),
            # Dominator: share of the team's receiving yards and TDs. The
            # standard prospect metric, applied to the NFL offense here.
            # Per-game like the other shares: on raw season totals a receiver
            # who played four games looks irrelevant rather than dominant.
            "dominator_rating": _pct(
                _avg(
                    _share(s.get("rec_yd"), team_rec_yd.get(team), games),
                    _share(s.get("rec_td"), team_rec_td.get(team), games),
                )
            )
            if meta.get("position") in RECEIVING_POSITIONS
            else None,
            "drops": s.get("rec_drop"),
            "broken_tackles": s.get("rush_btkl"),
            "ppr_points": _round(s.get("pts_ppr")),
            "ppg": _round(_safe_div(s.get("pts_ppr"), games)),
        }

    return out


def _racr(rec_yd: float | None, air_yd: float | None) -> float | None:
    """Receiver Air Conversion Ratio, defined only on positive air yards.

    Backs and quarterbacks routinely post *negative* completed air yards - a
    check-down caught two yards behind the line contributes -2. Dividing by a
    negative denominator yields a large negative ratio that looks like a
    catastrophic efficiency score when it is really a category error: RACR is a
    downfield-receiver metric and simply does not apply to them.
    """
    if not air_yd or air_yd <= 0 or rec_yd is None:
        return None
    return rec_yd / air_yd


def _wopr(target_share: float | None, air_share: float | None) -> float | None:
    """Weighted Opportunity Rating. Needs at least one of the two shares."""
    if target_share is None and air_share is None:
        return None
    return 1.5 * (target_share or 0.0) + 0.7 * (air_share or 0.0)


def _avg(*vals: float | None) -> float | None:
    present = [v for v in vals if v is not None]
    return sum(present) / len(present) if present else None


def _pct(v: float | None) -> float | None:
    return round(v * 100, 1) if v is not None else None


def _round(v: float | None) -> float | None:
    return round(v, 2) if isinstance(v, (int, float)) else None


def opportunity_flags(u: dict, position: str) -> list[str]:
    """Plain-language reads on a usage profile."""
    flags: list[str] = []
    if not u:
        return ["no prior-season usage data (rookie, or did not play)"]

    snap = u.get("snap_share")
    if snap is not None:
        if snap >= 75:
            flags.append(f"every-down role ({snap:.0f}% snaps)")
        elif snap <= 40:
            flags.append(f"rotational ({snap:.0f}% snaps) — role risk")

    ts = u.get("target_share")
    if ts is not None and position in ("WR", "TE"):
        if ts >= 25:
            flags.append(f"alpha target share ({ts:.0f}%)")
        elif ts <= 12:
            flags.append(f"peripheral in the passing game ({ts:.0f}% targets)")

    cs = u.get("carry_share")
    if cs is not None and position == "RB":
        if cs >= 55:
            flags.append(f"workhorse ({cs:.0f}% of team carries)")
        elif cs <= 30:
            flags.append(f"committee back ({cs:.0f}% of carries)")

    rz = u.get("rz_share")
    if rz is not None and rz >= 20:
        flags.append(f"red-zone focal point ({rz:.0f}%)")

    dpc = u.get("depth_per_catch")
    if dpc is not None and position in ("WR", "TE"):
        if dpc >= 11:
            flags.append(f"catches come downfield ({dpc:.1f} air yds/rec) — boom/bust")
        elif dpc <= 7:
            flags.append(
                f"short-area chain mover ({dpc:.1f} air yds/rec) — PPR-friendly floor"
            )

    yac = u.get("yac_per_catch")
    if yac is not None and yac >= 6:
        flags.append(f"dangerous after the catch ({yac:.1f} YAC/rec)")

    wopr = u.get("wopr_proxy")
    if wopr is not None and position in ("WR", "TE"):
        if wopr >= 0.70:
            flags.append(f"elite opportunity share (WOPR {wopr:.2f})")
        elif wopr >= 0.50:
            flags.append(f"starter-level opportunity (WOPR {wopr:.2f})")

    racr = u.get("racr")
    if racr is not None and position in ("WR", "TE") and u.get("targets", 0) >= 40:
        if racr >= 1.4:
            flags.append(f"creates after the catch (RACR {racr:.2f})")
        elif racr <= 0.8:
            flags.append(f"inefficient on his air yards (RACR {racr:.2f})")

    dom = u.get("dominator_rating")
    if dom is not None and dom >= 30 and position in ("WR", "TE"):
        flags.append(f"dominates the passing game ({dom:.0f}% dominator rating)")

    if u.get("games") and u["games"] <= 12:
        flags.append(f"only {u['games']:.0f} games played — availability risk")

    return flags or ["unremarkable usage profile"]
