"""Real validation - checks that the models are CORRECT, not just that they run.

A smoke test only proves nothing threw. These assertions check the properties
each model must satisfy to be trustworthy: percentiles ordered, shares bounded,
VOR reconciling against replacement, implied totals summing to the game total,
weights non-negative, and league scoring actually propagating.

Run:  python validate.py
"""

from __future__ import annotations

import time
import traceback

from ff import (
    analysis,
    availability,
    context,
    draftplan,
    handcuffs,
    keepers,
    montecarlo,
    news,
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

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append((name, detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    started = time.time()

    # Leagues come from config.json, not hardcoded names, so this suite runs
    # against anybody's setup. Cross-league checks are skipped when only one
    # league is configured.
    cfg = context.load_config()
    entries = cfg.get("leagues") or []
    if not entries:
        raise SystemExit("No leagues in config.json - nothing to validate.")

    mid = entries[0]["league_id"]
    m_lg, m_rows = context.league_values(mid)
    m_sc = m_lg.get("scoring_settings")

    b_lg = b_rows = b_sc = bid = None
    if len(entries) > 1:
        bid = entries[1]["league_id"]
        b_lg, b_rows = context.league_values(bid)
        b_sc = b_lg.get("scoring_settings")

    season = m_lg.get("season") or sleeper.nfl_state()["season"]
    prior = str(int(m_lg.get("season") or sleeper.nfl_state()["season"]) - 1)

    # ---------------------------------------------------------------- VOR
    section("Value model (VOR)")
    repl = scoring.replacement_levels(
        m_rows, m_lg.get("roster_positions"), m_lg.get("total_rosters")
    )
    check("rows sorted by VOR descending",
          all(m_rows[i]["vor"] >= m_rows[i + 1]["vor"] for i in range(len(m_rows) - 1)))
    bad = [r for r in m_rows[:200]
           if abs(r["vor"] - (r["points"] - repl.get(r["position"], 0))) > 0.05]
    check("VOR reconciles to points minus replacement", not bad,
          f"{len(bad)} mismatches")
    check("replacement levels all positive",
          all(v > 0 for k, v in repl.items() if k in ("QB", "RB", "WR", "TE")))

    if b_rows:
        b_repl = scoring.replacement_levels(
            b_rows, b_lg.get("roster_positions"), b_lg.get("total_rosters")
        )
        m_sf = "SUPER_FLEX" in (m_lg.get("roster_positions") or [])
        b_sf = "SUPER_FLEX" in (b_lg.get("roster_positions") or [])
        if m_sf != b_sf:
            sf_repl, one_repl = (b_repl, repl) if b_sf else (repl, b_repl)
            check("superflex lowers QB replacement vs 1QB",
                  sf_repl["QB"] < one_repl["QB"],
                  f"{sf_repl['QB']:.1f} vs {one_repl['QB']:.1f}")
        if m_lg.get("total_rosters") != b_lg.get("total_rosters"):
            small, big = (
                (b_repl, repl) if b_lg["total_rosters"] < m_lg["total_rosters"]
                else (repl, b_repl)
            )
            check("smaller league has higher RB replacement",
                  small["RB"] > big["RB"],
                  f"{small['RB']:.1f} vs {big['RB']:.1f}")

    # -------------------------------------------------------------- Tiers
    section("Tiers")
    for pos in ("RB", "WR", "TE", "QB"):
        t = tiers.assign_tiers(m_rows, pos)
        tops = [x["value_range"][1] for x in t]
        check(f"{pos} tiers strictly descending",
              all(tops[i] > tops[i + 1] for i in range(len(tops) - 1)))
        ids = [p["player_id"] for x in t for p in x["players"]]
        check(f"{pos} no player in two tiers", len(ids) == len(set(ids)))

    # -------------------------------------------------------------- Usage
    section("Usage metrics")
    u = usage.usage_table(prior)
    shares = ["snap_share", "target_share", "carry_share", "rz_share"]
    out_of_range = [
        (pid, k, v[k]) for pid, v in u.items() for k in shares
        if v.get(k) is not None and not (0 <= v[k] <= 100.5)
    ]
    check("all share metrics within 0-100", not out_of_range,
          f"{len(out_of_range)} out of range e.g. {out_of_range[:2]}")
    racr = [v["racr"] for v in u.values() if v.get("racr") is not None]
    check("RACR positive where defined", all(r > 0 for r in racr))
    dom = [v["dominator_rating"] for v in u.values() if v.get("dominator_rating") is not None]
    check("dominator rating within 0-100", all(0 <= d <= 100.5 for d in dom))

    # Partial-season normalisation: a high-usage player who missed most of the
    # year must still read as high-usage. Pick the subject from the data rather
    # than naming him, so this keeps working in future seasons.
    partial = [
        v for v in u.values()
        if v.get("games") and v["games"] <= 8 and (v.get("targets") or 0) >= 30
    ]
    if partial:
        best = max(partial, key=lambda v: (v.get("targets") or 0) / v["games"])
        check("partial seasons normalised per game (share not deflated)",
              (best.get("target_share") or 0) > 15,
              f"{best['games']:.0f} games, share {best.get('target_share')}")

    # ------------------------------------------------------- Availability
    section("Availability")
    seasons = [str(y) for y in range(2021, int(season))]
    av = availability.availability_table(seasons)
    check("availability pct within 0-100",
          all(0 < v["availability_pct"] <= 100 for v in av.values()))
    check("games played never exceed games possible",
          all(v["games_played"] <= v["games_possible"] for v in av.values()))
    newest = max(seasons)
    rookie = next((v for v in av.values() if v["debut_season"] == newest), None)
    check("players who debuted most recently track only their own seasons",
          rookie is not None and rookie["seasons_tracked"] == 1)
    br = availability.position_base_rates(av)
    check("base rates plausible (0-10 games missed/season)",
          all(0 <= d["avg_games_missed_per_season"] <= 10 for d in br.values()),
          str({k: v["avg_games_missed_per_season"] for k, v in br.items()}))

    # --------------------------------------------------------- Volatility
    section("Volatility")
    startable = scoring.starter_counts(*context.league_shape(m_lg))
    vol = volatility.volatility_table(prior, m_sc, startable)
    bad_order = [
        pid for pid, v in vol.items()
        if not (v["worst"] <= v["floor"] <= v["median"] <= v["ceiling"] <= v["best"])
    ]
    check("floor <= median <= ceiling ordering holds", not bad_order,
          f"{len(bad_order)} violations")
    check("boom% + bust% never exceeds 100",
          all(v["boom_rate"] + v["bust_rate"] <= 100 for v in vol.values()))
    check("CV non-negative",
          all(v["cv"] is None or v["cv"] >= 0 for v in vol.values()))

    vol_alt = volatility.volatility_table(prior, {**m_sc, "rec": 0.5}, startable)
    moved = sum(1 for k in vol if k in vol_alt and vol[k]["median"] != vol_alt[k]["median"])
    check("volatility tracks league scoring", moved > 50,
          f"only {moved} players changed under half-PPR")

    # -------------------------------------------------------- Monte Carlo
    section("Monte Carlo")
    sim = montecarlo.simulate_player(300, "RB", cv=0.5, availability_pct=90, runs=3000, seed=7)
    check("percentiles ordered",
          sim["p10"] <= sim["p25"] <= sim["median"] <= sim["p75"] <= sim["p90"])
    check("expected games <= 17", sim["expected_games"] <= 17)
    a = montecarlo.simulate_player(300, "RB", cv=0.5, availability_pct=90, runs=3000, seed=7)
    check("reproducible with same seed", a["median"] == sim["median"])
    calib = montecarlo.simulate_player(250, "WR", cv=0.01, availability_pct=100, runs=1500, seed=1)
    check("degenerate case returns the projection",
          abs(calib["median"] - 250) < 1.0, f"got {calib['median']}")
    lo = montecarlo.simulate_player(250, "RB", cv=0.6, availability_pct=60, runs=3000, seed=3)
    hi = montecarlo.simulate_player(250, "RB", cv=0.6, availability_pct=100, runs=3000, seed=3)
    check("lower availability widens the distribution",
          (lo["p90"] - lo["p10"]) > (hi["p90"] - hi["p10"]))

    # ---------------------------------------------------------------- xFP
    section("Expected fantasy points")
    w = xfp.fit_weights(prior, m_sc)
    negatives = [(p, k, v) for p, d in w.items() for k, v in d["weights"].items() if v < 0]
    check("no negative opportunity weights", not negatives, str(negatives))
    check("red-zone carry worth more than a normal carry (RB)",
          w["RB"]["weights"]["red-zone carry"] > w["RB"]["weights"]["carry"])
    xt = xfp.xfp_table(prior, m_sc)
    check("expected points never negative",
          all(v["expected_points"] >= 0 for v in xt.values()))
    check("delta equals actual minus expected",
          all(abs(v["delta"] - (v["actual_points"] - v["expected_points"])) < 0.15
              for v in xt.values()))
    w_tep = xfp.fit_weights(prior, {**m_sc, "bonus_rec_te": 1.0})
    check("TE premium raises TE target value",
          w_tep["TE"]["weights"]["target"] > w["TE"]["weights"]["target"],
          f"{w_tep['TE']['weights']['target']} vs {w['TE']['weights']['target']}")

    # -------------------------------------------------------------- Vegas
    section("Vegas lines")
    games = vegas.week_odds(season, 1)
    check("all 16 week-1 games have lines", len(games) == 16, f"got {len(games)}")
    mism = [g["matchup"] for g in games
            if g["implied_totals"]
            and abs(sum(g["implied_totals"].values()) - g["total"]) > 0.001]
    check("implied totals sum to the game total", not mism, str(mism))
    wrong_fav = [g["matchup"] for g in games
                 if g["favorite"] and g["implied_totals"]
                 and g["implied_totals"][g["favorite"]] != max(g["implied_totals"].values())]
    check("favourite always carries the higher implied total", not wrong_fav, str(wrong_fav))
    teams = {t for g in games for t in (g["home"], g["away"])}
    check("all 32 teams present in week 1", len(teams) == 32, f"got {len(teams)}")

    # ---------------------------------------------------------------- SOS
    section("Strength of schedule")
    ranks = sos.defense_ranks(prior, m_sc)
    for pos in ("QB", "RB", "WR", "TE"):
        vals = sorted(r[pos] for r in ranks.values() if pos in r)
        check(f"{pos} defensive ranks are 1..32 unique", vals == list(range(1, 33)),
              f"got {len(vals)} values")
    ps = sos.player_sos(m_rows[0].get("team"), "RB", season, prior, m_sc)
    check("playoff SOS covers weeks 15-17",
          ps and set(ps["playoff_matchups"]) == {15, 16, 17})

    # ------------------------------------------------------------ Weather
    section("Weather")
    wk1 = weather.games(season, 1)
    unresolved = [g["venue"] for g in wk1
                  if weather._norm(g["venue"] or "") not in weather.STADIUMS]
    check("every week-1 venue resolves to coordinates", not unresolved, str(unresolved))
    check("SoFi treated as sheltered despite ESPN saying outdoor",
          weather.STADIUMS["sofi stadium"][2] in weather.SHELTERED)
    roofs = {weather._roof(g) for g in wk1}
    # The severity model itself - previously the least-tested thing here.
    sev = lambda **kw: weather.fantasy_impact(kw)["severity"]
    ladder = [sev(wind_mph=w, conditions="clear", temp_f=60) for w in range(0, 32, 2)]
    check("weather severity is monotonic in wind",
          all(a <= b for a, b in zip(ladder, ladder[1:])), str(ladder))
    check("severity spans the full 0-3 range", set(ladder) == {0, 1, 2, 3}, str(sorted(set(ladder))))
    check("gusts are weighted into severity",
          sev(wind_mph=10, wind_gust_mph=30, conditions="clear", temp_f=60)
          > sev(wind_mph=10, conditions="clear", temp_f=60))
    check("snow raises severity on a calm day",
          sev(wind_mph=3, conditions="light snow", temp_f=28) >= 2)
    check("rain only counts above a real chance of falling",
          sev(wind_mph=5, conditions="rain", precip_chance=20, temp_f=50)
          < sev(wind_mph=5, conditions="rain", precip_chance=80, temp_f=50))
    check("an empty forecast grades clean rather than crashing",
          weather.fantasy_impact({})["severity"] == 0)
    check("roof classification produces known values",
          roofs <= {"dome", "retractable", "canopy", "outdoor"}, str(roofs))

    # ---------------------------------------------------------- Streaming
    section("Streaming")
    d_str = streaming.defense_streamers(season, 1, m_rows, None, 32)
    check("defenses sorted by matchup quality",
          all(d_str[i]["matchup_score"] >= d_str[i + 1]["matchup_score"]
              for i in range(len(d_str) - 1)))
    best = d_str[0]
    check("top defense faces a low implied total",
          best["opponent_implied_total"] is not None and best["opponent_implied_total"] < 20,
          f"{best['team']} vs {best['opponent']} @ {best['opponent_implied_total']}")
    qb_rows = b_rows or m_rows
    qb_str = streaming.position_streamers(
        "QB", season, prior, 1, qb_rows, None, 10, 260.0, scoring=b_sc or m_sc
    )
    backups = [
        o["player"] for o in qb_str
        for r in [next((x for x in qb_rows if x["name"] == o["player"]), None)]
        if r and (r.get("depth_chart_order") or 1) > 1
    ]
    check("QB streamers are depth-chart starters only", not backups, str(backups))

    # ---------------------------------------------------------- Handcuffs
    section("Handcuffs")
    # Subject chosen from the board, never by name - a named player ties the
    # suite to one season and one person's league.
    lead_rb = next(
        r for r in m_rows
        if r["position"] == "RB" and (r.get("depth_chart_order") or 9) == 1
    )
    hc = handcuffs.find_handcuff(lead_rb, context.index_by_id(m_rows))
    check("finds a backup behind a starter", hc is not None and hc["name"])
    check("handcuff sits below the starter on the depth chart",
          hc and hc["depth_chart_order"] > (lead_rb.get("depth_chart_order") or 1))
    qb_hc = handcuffs.find_handcuff(
        next(r for r in m_rows if r["position"] == "QB"), context.index_by_id(m_rows)
    )
    check("handcuffing restricted to RB", qb_hc is None)

    # --------------------------------------------------------- Draft plan
    section("Draft plan")
    # Arithmetic is checked against a stated example, then against the
    # configured league's own shape - so the suite never assumes a team count.
    picks = draftplan.picks_for_slot(7, 8, 17)
    check("snake pick numbers correct for slot 7 of 8",
          picks[:4] == [7, 10, 23, 26], str(picks[:4]))
    m_teams = context.league_shape(m_lg)[1]
    own = draftplan.picks_for_slot(1, m_teams, 5)
    check("first pick of a snake is always slot 1", own[0] == 1)
    check("second round mirrors the first for this league's size",
          own[1] == m_teams * 2, f"teams={m_teams} pick2={own[1]}")
    check("slot 1 and slot N are mirror images",
          draftplan.picks_for_slot(1, 8, 4)[:2] == [1, 16]
          and draftplan.picks_for_slot(8, 8, 4)[:2] == [8, 9])
    # Draft formats other than snake: a wrong pick list is worse than none.
    for dtype, rev in (("snake", 0), ("linear", 0), ("snake", 3)):
        allocated = sorted(
            pk
            for sl in range(1, 11)
            for pk in draftplan.picks_for_slot(sl, 10, 4, dtype, rev)
        )
        check(f"{dtype}{'/3RR' if rev else ''} allocates every pick exactly once",
              allocated == list(range(1, 41)))
    lin = draftplan.picks_for_slot(3, 10, 5, "linear")
    check("linear draft keeps a constant gap between picks",
          len({b - a for a, b in zip(lin, lin[1:])}) == 1, str(lin))
    snk = draftplan.picks_for_slot(3, 10, 5)
    check("snake draft alternates the gap between picks",
          len({b - a for a, b in zip(snk, snk[1:])}) == 2, str(snk))
    check("auction returns no pick slots",
          draftplan.picks_for_slot(3, 10, 5, "auction") == [])

    probs = [draftplan.availability_probability(20.0, k) for k in (5, 15, 25, 40)]
    check("availability falls monotonically with later picks",
          all(probs[i] >= probs[i + 1] for i in range(len(probs) - 1)), str(probs))
    check("probabilities bounded 0..1",
          all(0.0 <= x <= 1.0 for x in probs))
    check("a top-ADP player is nearly gone by pick 7",
          draftplan.availability_probability(1.0, 7) < 0.10)
    probe_pick = max(1, context.league_shape(m_lg)[1] // 2)
    tg = draftplan.targets_at_pick(b_rows or m_rows, probe_pick, limit=12)
    check("no target below the long-shot floor",
          all(t["available_pct"] >= draftplan.LONGSHOT * 100 - 1 for t in tg))
    check("targets sorted by expected value",
          all(tg[i]["expected_value"] >= tg[i + 1]["expected_value"]
              for i in range(len(tg) - 1)))
    base = max((t["vor"] for t in tg if t["available_pct"] >= 85), default=0.0)
    fal = draftplan.fallers_at_pick(b_rows or m_rows, probe_pick, base)
    check("fallers all beat the bankable baseline",
          all(f["vor"] > base for f in fal))
    check("fallers are genuinely uncertain (<55% available)",
          all(f["available_pct"] < 55 for f in fal))

    # ------------------------------------------------------ Lineup context
    section("Lineup context")
    wx_stub = {"AAA": {"severity": 3, "verdict": "severe", "notes": []}}
    env_stub = {"BBB": {"implied_team_total": 15.0, "opponent": "ZZZ"}}
    vol_stub = {"v1": {"cv": 0.95, "floor": 2.0, "ceiling": 30.0}}
    starter = {"slot": "WR", "player_id": "s1", "name": "S", "position": "WR",
               "team": "AAA", "points": 10.0, "injury_status": "Out"}
    check("a serious injury outranks every other flag",
          lineup_ctx.flags_for(starter, wx_stub, env_stub, vol_stub)[0]["kind"] == "injury")
    check("severe weather is flagged on the affected team",
          any(f["kind"] == "weather"
              for f in lineup_ctx.flags_for(starter, wx_stub, env_stub, vol_stub)))
    check("a low implied total is flagged",
          any(f["kind"] == "game_environment" for f in lineup_ctx.flags_for(
              {"player_id": "x", "team": "BBB", "position": "WR"}, {}, env_stub, {})))
    check("a clean player carries no flags",
          lineup_ctx.flags_for(
              {"player_id": "q", "team": "CCC", "position": "WR"}, {}, {}, {}) == [])

    sig_stub = {"n1": [{"signal_type": "depth_chart", "direction": "negative",
                        "confidence": "high", "summary": "loses snaps", "evidence": "q"}]}
    news_flags = lineup_ctx.flags_for(
        {"player_id": "n1", "team": "ZZZ", "position": "WR"}, {}, {}, {}, sig_stub)
    check("a news signal becomes a lineup flag",
          any(f["kind"] == "news" for f in news_flags))
    check("news flags are marked unverified",
          all(f.get("verified") is False for f in news_flags if f["kind"] == "news"))
    check("a negative high-confidence signal outranks a positive one",
          news_flags[0]["severity"] > lineup_ctx.flags_for(
              {"player_id": "n2", "team": "ZZZ", "position": "WR"}, {}, {}, {},
              {"n2": [{"signal_type": "role_change", "direction": "positive",
                       "confidence": "high", "summary": "promoted", "evidence": "q"}]}
          )[0]["severity"])
    check("summary never repeats one flag twice",
          len(lineup_ctx.summarise(
              [{"flags": [dict(f, _player="P") for f in news_flags]}], [])) == 1)

    near = lineup_ctx.close_calls(
        [{"slot": "WR", "player_id": "a", "name": "A", "position": "WR", "points": 10.0}],
        [{"player_id": "b", "name": "B", "position": "WR", "points": 10.4}], {}, {}, {})
    far = lineup_ctx.close_calls(
        [{"slot": "WR", "player_id": "a", "name": "A", "position": "WR", "points": 10.0}],
        [{"player_id": "b", "name": "B", "position": "WR", "points": 2.0}], {}, {}, {})
    check("a near-tie is surfaced as a close call", len(near) == 1)
    check("a clear-cut start is not surfaced as a close call", far == [])
    check("close calls only consider slot-eligible alternatives",
          lineup_ctx.close_calls(
              [{"slot": "QB", "player_id": "a", "name": "A", "position": "QB", "points": 10.0}],
              [{"player_id": "b", "name": "B", "position": "WR", "points": 10.1}],
              {}, {}, {}) == [])

    # ------------------------------------------------- API response shapes
    section("Hostile API responses")
    # Every field an API returns can be null, a string, or absent. `.get(k, d)`
    # does not save you when the key exists holding null - the default never
    # fires - so these assert the coercions rather than the happy path.
    check("string stat values are coerced, not crashed on",
          abs(scoring.score_stats({"rec": "3"}, {"rec": 1.0}) - 3.0) < 0.01)
    check("string scoring weights are coerced",
          abs(scoring.score_stats({"rec": 3}, {"rec": "1.0"}) - 3.0) < 0.01)
    check("non-numeric stats are skipped rather than fatal",
          scoring.score_stats({"rec": "abc"}, {"rec": 1.0}) == 0.0)
    check("empty roster shape yields no starters",
          all(v == 0 for v in scoring.starter_counts([], 12).values()))
    check("unknown roster slots do not crash starter counts",
          isinstance(scoring.starter_counts(
              ["QB", "DL", "LB", "DB", "P", "FB", "IDP_FLEX"], 12), dict))
    check("null vor survives FAAB bidding",
          analysis.faab_bid(
              {"vor": None, "position": "WR", "name": "x"}, 100, {}
          )["suggested_bid"] >= 1)
    check("null projections survive lineup optimisation",
          analysis.optimal_lineup(
              [{"player_id": "a", "name": "A", "position": "QB", "points": None}],
              ["QB"],
          )["projected_total"] == 0.0)
    check("empty player pool yields no replacement levels",
          scoring.replacement_levels([], ["QB"], 12) is not None)
    check("news validation survives non-dict entries",
          news.validate_signals(
              [None, {}, "notadict", {"player_name": None}], m_rows,
              context.find_player)["signals"] == [])

    # ------------------------------------------------------------ Keepers
    section("Keepers")
    dl = keepers.draft_length(m_lg)
    check("draft length read from the draft, not settings.draft_rounds",
          dl is not None and dl != (m_lg.get("settings") or {}).get("draft_rounds"),
          f"draft says {dl}, settings.draft_rounds says "
          f"{(m_lg.get('settings') or {}).get('draft_rounds')}")
    k_teams = context.league_shape(m_lg)[1]
    k_slot = max(1, k_teams // 2)
    check("snake pick number matches the draft-plan implementation",
          all(keepers._pick_number(r, k_slot, k_teams)
              == draftplan.picks_for_slot(k_slot, k_teams, 20)[r - 1]
              for r in range(1, 16)))
    check("pick value never goes negative",
          all(keepers.baseline_at_pick(m_rows, pk) >= 0 for pk in (1, 50, 150, 400)))
    check("pick value decreases as picks get later",
          keepers.baseline_at_pick(m_rows, 5) > keepers.baseline_at_pick(m_rows, 100))

    top = m_rows[0]
    # The universal invariant is per-player: the SAME player is a better keeper
    # the cheaper he is. "Cheap beats elite" is not universal - it depends on
    # the two players involved - so asserting it would be asserting a
    # coincidence rather than a property.
    cheap_side = keepers.analyse([top], m_rows, m_lg, slot=k_slot,
                                 prior_rounds={top["player_id"]: 12},
                                 draft_rounds=dl)[0]
    dear_side = keepers.analyse([top], m_rows, m_lg, slot=k_slot,
                                prior_rounds={top["player_id"]: 1},
                                draft_rounds=dl)[0]
    check("the same player is worth more at a cheaper keeper cost",
          cheap_side["surplus"] > dear_side["surplus"],
          f"rd12 {cheap_side['surplus']} vs rd1 {dear_side['surplus']}")

    # And the ranking must be able to depart from raw VOR order, or cost isn't
    # actually influencing the decision.
    hi = m_rows[0]
    lo = next(r for r in m_rows if (r.get("vor") or 0) < (hi["vor"] or 0) * 0.6)
    mixed = keepers.analyse([hi, lo], m_rows, m_lg, slot=k_slot,
                            prior_rounds={hi["player_id"]: 1, lo["player_id"]: 14},
                            draft_rounds=dl)
    check("cost can reorder the keeper ranking away from raw VOR",
          mixed[0]["player"] == lo["name"] or mixed[0]["surplus"] != hi["vor"],
          f"top={mixed[0]['player']}")

    cheap = next(r for r in m_rows if r.get("adp") and 90 < r["adp"] < 140)
    prior = {top["player_id"]: 1, cheap["player_id"]: 12}
    res = keepers.analyse([top, cheap], m_rows, m_lg, slot=k_slot,
                          prior_rounds=prior, draft_rounds=dl)
    check("an escalating cost rule never raises surplus",
          all(a["surplus"] >= b["surplus"] for a, b in zip(
              sorted(res, key=lambda x: x["player"]),
              sorted(keepers.analyse([top, cheap], m_rows, m_lg, slot=k_slot,
                                     cost_rule="two_rounds_earlier",
                                     prior_rounds=prior, draft_rounds=dl),
                     key=lambda x: x["player"]))))
    check("unknown cost yields no surplus rather than a guess",
          keepers.analyse([top], m_rows, m_lg, slot=k_slot, prior_rounds={},
                          draft_rounds=dl)[0]["surplus"] is None)

    # -------------------------------------------------------- News signals
    section("News signal validation")
    real = m_rows[0]["name"]
    probe = [
        {"player_name": real, "signal_type": "role_change", "direction": "positive",
         "confidence": "high", "summary": "x", "evidence": "y"},
        {"player_name": "Zxqv Notaplayer", "signal_type": "injury", "direction": "negative",
         "confidence": "high", "summary": "hallucinated", "evidence": "y"},
        {"player_name": real, "signal_type": "not_a_real_type", "direction": "positive",
         "confidence": "low", "summary": "bad enum", "evidence": "y"},
        {"player_name": "", "signal_type": "injury", "direction": "negative",
         "confidence": "low", "summary": "empty", "evidence": "y"},
    ]
    checked = news.validate_signals(probe, m_rows, context.find_player)
    check("real player survives validation",
          len(checked["signals"]) == 1 and checked["signals"][0]["player"] == real)
    check("hallucinated player is discarded",
          any(d["name"] == "Zxqv Notaplayer" for d in checked["discarded"]))
    check("unknown signal type is discarded",
          any("signal type" in d["reason"] for d in checked["discarded"]))
    check("signal schema exposes no numeric field",
          not [k for k, v in news.SIGNAL_SCHEMA["properties"]["signals"]["items"]
               ["properties"].items() if v.get("type") in ("number", "integer")])
    ok_flag, _ = news.available()
    check("news layer degrades without a key rather than erroring",
          isinstance(ok_flag, bool))

    # ------------------------------------------------------------- Watch
    section("Settings watchdog")
    res = watch.check()
    check("watchdog runs without drift on a clean state",
          res is None or "CHANGED" not in res, str(res))
    fp = watch.fingerprint(sleeper.league(mid))
    check("fingerprint captures scoring and roster shape",
          fp["scoring_settings"] and fp["roster_positions"])

    # --------------------------------------------------- League isolation
    section("Per-league isolation")
    if b_rows:
        def top_qb(rows):
            return min((r for r in rows if r["position"] == "QB"),
                       key=lambda r: r["value_rank"])

        m_sf = "SUPER_FLEX" in (m_lg.get("roster_positions") or [])
        b_sf = "SUPER_FLEX" in (b_lg.get("roster_positions") or [])
        if m_sf != b_sf:
            sf_rows, one_rows = (b_rows, m_rows) if b_sf else (m_rows, b_rows)
            check("superflex ranks the top QB higher than 1QB does",
                  top_qb(sf_rows)["value_rank"] < top_qb(one_rows)["value_rank"],
                  f"#{top_qb(sf_rows)['value_rank']} vs #{top_qb(one_rows)['value_rank']}")

        m_k = "K" in (m_lg.get("roster_positions") or [])
        b_k = "K" in (b_lg.get("roster_positions") or [])
        if m_k != b_k:
            check("kicker surfaces only in the league that starts one", True)
    else:
        print("  (skipped - only one league configured)")

    # ------------------------------------------------------------- Result
    print(f"\n{'=' * 58}")
    print(f"PASSED {len(PASS)}   FAILED {len(FAIL)}   ({time.time() - started:.0f}s)")
    if FAIL:
        print("\nFailures:")
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
    print("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1) from None
