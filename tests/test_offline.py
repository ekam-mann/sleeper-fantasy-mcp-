"""Tests that need no network and no configured league.

`validate.py` is the real correctness suite, but it calls live APIs and needs a
configured league, so it cannot run in CI. These cover the pure logic - the
maths, the parsing, and the handling of every shape an API can return - so a
pull request still gets meaningful automated checking.

Run: pytest
"""

from __future__ import annotations

import pytest

from ff import (
    analysis,
    construction,
    draftplan,
    keepers,
    lineup,
    montecarlo,
    news,
    scoring,
    usage,
    weather,
)

# --------------------------------------------------------------------- scoring


def test_scoring_applies_league_settings():
    assert scoring.score_stats({"rec": 3}, {"rec": 1.0}) == pytest.approx(3.0)
    assert scoring.score_stats({"rec": 3}, {"rec": 0.5}) == pytest.approx(1.5)


@pytest.mark.parametrize(
    "stats,scoring_cfg",
    [
        ({"rec": "3"}, {"rec": 1.0}),  # string stat
        ({"rec": 3}, {"rec": "1.0"}),  # string weight
    ],
)
def test_string_numbers_are_coerced(stats, scoring_cfg):
    """JSON does not guarantee numbers arrive as numbers."""
    assert scoring.score_stats(stats, scoring_cfg) == pytest.approx(3.0)


def test_non_numeric_stats_are_skipped_not_fatal():
    assert scoring.score_stats({"rec": "abc"}, {"rec": 1.0}) == 0.0


def test_empty_inputs_are_safe():
    assert scoring.score_stats({}, {}) == 0.0
    assert all(v == 0 for v in scoring.starter_counts([], 12).values())


def test_unknown_roster_slots_do_not_crash():
    exotic = ["QB", "DL", "LB", "DB", "P", "FB", "IDP_FLEX", "BN"]
    assert isinstance(scoring.starter_counts(exotic, 12), dict)


def test_flex_deepens_replacement_demand():
    """More flex spots mean more startable RB/WR league-wide."""
    no_flex = scoring.starter_counts(["QB", "RB", "WR"], 12)
    with_flex = scoring.starter_counts(["QB", "RB", "WR", "FLEX", "FLEX"], 12)
    assert with_flex["RB"] > no_flex["RB"]


def test_superflex_creates_qb_demand():
    one_qb = scoring.starter_counts(["QB", "RB", "WR"], 12)
    superflex = scoring.starter_counts(["QB", "RB", "WR", "SUPER_FLEX"], 12)
    assert superflex["QB"] > one_qb["QB"]


def test_every_started_position_is_counted_not_just_skill_positions():
    """K, DEF and IDP slots are real starters, not an afterthought."""
    counts = scoring.starter_counts(
        ["QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB", "BN"], 12
    )
    for pos in ("K", "DEF", "DL", "LB", "DB"):
        assert counts.get(pos) == 12


def test_position_count_scales_with_slots():
    one = scoring.starter_counts(["QB", "K", "DEF"], 10)
    two = scoring.starter_counts(["QB", "K", "K", "DEF"], 10)
    assert one["K"] == 10
    assert two["K"] == 20


def test_position_the_league_never_starts_has_no_slots():
    counts = scoring.starter_counts(["QB", "RB", "WR", "TE", "DEF"], 12)
    assert "K" not in counts


def test_unstarted_position_grades_at_or_below_zero():
    """A kicker cannot have positive value in a league with no kicker slot.

    The old code assumed one starter per team for anything outside QB/RB/WR/TE,
    so kickers kept a positive baseline in leagues that never start one.
    """
    kickers = [{"position": "K", "points": float(p)} for p in (150, 140, 130, 120)]
    levels = scoring.replacement_levels(kickers, ["QB", "RB", "WR", "TE"], 12)
    assert levels["K"] == 150  # the best one is the baseline
    assert all(k["points"] - levels["K"] <= 0 for k in kickers)


def test_idp_flex_distributes_across_defensive_positions():
    counts = scoring.starter_counts(["QB", "IDP_FLEX", "IDP_FLEX"], 10)
    assert sum(counts.get(p, 0) for p in ("DL", "LB", "DB")) == pytest.approx(20.0)


def test_unknown_slot_types_still_count_as_starters():
    counts = scoring.starter_counts(["QB", "P", "FB"], 8)
    assert counts["P"] == 8 and counts["FB"] == 8


# ------------------------------------------------------------------ draftplan


@pytest.mark.parametrize("dtype,reversal", [("snake", 0), ("linear", 0), ("snake", 3)])
def test_every_pick_allocated_exactly_once(dtype, reversal):
    teams, rounds = 10, 4
    allocated = sorted(
        pick
        for slot in range(1, teams + 1)
        for pick in draftplan.picks_for_slot(slot, teams, rounds, dtype, reversal)
    )
    assert allocated == list(range(1, teams * rounds + 1))


def test_linear_gap_is_constant_snake_alternates():
    lin = draftplan.picks_for_slot(3, 10, 5, "linear")
    snake = draftplan.picks_for_slot(3, 10, 5, "snake")
    assert len({b - a for a, b in zip(lin, lin[1:])}) == 1
    assert len({b - a for a, b in zip(snake, snake[1:])}) == 2


def test_auction_has_no_pick_slots():
    assert draftplan.picks_for_slot(3, 10, 5, "auction") == []


def test_first_pick_is_always_certain():
    """Nobody can be drafted before pick 1, whatever their ADP.

    The distribution of draft position is left-truncated at the first pick.
    Untruncated, a normal around an ADP of 1.0 puts half its mass below pick 1
    and reported that player as only 50% likely to survive to the very first
    pick of the draft.
    """
    for adp in (1.0, 1.5, 2.0, 5.0, 20.0, 100.0, 300.0):
        assert draftplan.availability_probability(adp, 1) == pytest.approx(1.0)


@pytest.mark.parametrize("adp", [1.0, 3.0, 12.0, 50.0, 150.0])
def test_availability_is_bounded_and_monotonic(adp):
    probs = [draftplan.availability_probability(adp, p) for p in range(1, 200)]
    assert all(0.0 <= x <= 1.0 for x in probs)
    assert all(a >= b - 1e-12 for a, b in zip(probs, probs[1:]))


def test_no_probability_mass_before_the_draft_starts():
    """A pick at or before the first is certain, never a fraction."""
    for pick in (0, 1):
        assert draftplan.availability_probability(1.0, pick) == pytest.approx(1.0)


def test_availability_falls_with_later_picks():
    probs = [draftplan.availability_probability(20.0, p) for p in (5, 15, 25, 40)]
    assert all(a >= b for a, b in zip(probs, probs[1:]))
    assert all(0.0 <= p <= 1.0 for p in probs)


# ----------------------------------------------------------------- montecarlo


def test_degenerate_simulation_returns_the_projection():
    sim = montecarlo.simulate_player(250, "WR", cv=0.01, availability_pct=100,
                                     runs=800, seed=1)
    assert abs(sim["median"] - 250) < 2.0


def test_percentiles_are_ordered():
    sim = montecarlo.simulate_player(300, "RB", cv=0.5, runs=800, seed=7)
    assert sim["p10"] <= sim["p25"] <= sim["median"] <= sim["p75"] <= sim["p90"]


def test_same_seed_is_reproducible():
    a = montecarlo.simulate_player(300, "RB", cv=0.5, runs=500, seed=7)
    b = montecarlo.simulate_player(300, "RB", cv=0.5, runs=500, seed=7)
    assert a["median"] == b["median"]


def test_lower_availability_widens_the_distribution():
    lo = montecarlo.simulate_player(250, "RB", cv=0.6, availability_pct=60,
                                    runs=800, seed=3)
    hi = montecarlo.simulate_player(250, "RB", cv=0.6, availability_pct=100,
                                    runs=800, seed=3)
    assert (lo["p90"] - lo["p10"]) > (hi["p90"] - hi["p10"])


# -------------------------------------------------------------------- weather


def test_severity_is_monotonic_in_wind():
    ladder = [
        weather.fantasy_impact({"wind_mph": w, "conditions": "clear", "temp_f": 60})[
            "severity"
        ]
        for w in range(0, 32, 2)
    ]
    assert all(a <= b for a, b in zip(ladder, ladder[1:]))
    assert set(ladder) == {0, 1, 2, 3}


def test_gusts_are_weighted():
    gusty = weather.fantasy_impact(
        {"wind_mph": 10, "wind_gust_mph": 30, "conditions": "clear", "temp_f": 60}
    )
    steady = weather.fantasy_impact(
        {"wind_mph": 10, "conditions": "clear", "temp_f": 60}
    )
    assert gusty["severity"] > steady["severity"]


def test_empty_forecast_grades_clean():
    assert weather.fantasy_impact({})["severity"] == 0


def test_sheltered_venues_are_excluded_from_weather():
    assert weather.STADIUMS["sofi stadium"][2] in weather.SHELTERED


# --------------------------------------------------------------------- lineup


def test_serious_injury_outranks_other_flags():
    player = {"player_id": "p", "team": "AAA", "position": "WR",
              "injury_status": "Out"}
    wx = {"AAA": {"severity": 3, "verdict": "severe", "notes": []}}
    assert lineup.flags_for(player, wx, {}, {})[0]["kind"] == "injury"


def test_news_flags_are_marked_unverified():
    signals = {"n": [{"signal_type": "depth_chart", "direction": "negative",
                      "confidence": "high", "summary": "loses snaps",
                      "evidence": "q"}]}
    flags = lineup.flags_for(
        {"player_id": "n", "team": "Z", "position": "WR"}, {}, {}, {}, signals
    )
    news_flags = [f for f in flags if f["kind"] == "news"]
    assert news_flags and all(f["verified"] is False for f in news_flags)


def test_clean_player_has_no_flags():
    assert lineup.flags_for({"player_id": "q", "team": "Z", "position": "WR"},
                            {}, {}, {}) == []


def test_close_call_only_when_projections_are_near():
    starter = [{"slot": "WR", "player_id": "a", "name": "A", "position": "WR",
                "points": 10.0}]
    near = [{"player_id": "b", "name": "B", "position": "WR", "points": 10.4}]
    far = [{"player_id": "b", "name": "B", "position": "WR", "points": 2.0}]
    assert len(lineup.close_calls(starter, near, {}, {}, {})) == 1
    assert lineup.close_calls(starter, far, {}, {}, {}) == []


def test_close_calls_respect_slot_eligibility():
    qb_slot = [{"slot": "QB", "player_id": "a", "name": "A", "position": "QB",
                "points": 10.0}]
    wr_bench = [{"player_id": "b", "name": "B", "position": "WR", "points": 10.1}]
    assert lineup.close_calls(qb_slot, wr_bench, {}, {}, {}) == []


# -------------------------------------------------------------------- keepers


def test_pick_value_never_negative():
    rows = [{"vor": -50.0, "adp": 200.0}, {"vor": 10.0, "adp": 5.0}]
    assert keepers.baseline_at_pick(rows, 250) >= 0


def test_cheaper_keeper_cost_is_worth_more():
    rows = [{"player_id": "x", "name": "X", "position": "RB", "vor": 100.0,
             "adp": 5.0, "points": 200.0}]
    lg = {"total_rosters": 12}
    cheap = keepers.analyse(rows, rows, lg, slot=1,
                            prior_rounds={"x": 12}, draft_rounds=15)[0]
    dear = keepers.analyse(rows, rows, lg, slot=1,
                           prior_rounds={"x": 1}, draft_rounds=15)[0]
    assert cheap["surplus"] > dear["surplus"]


def test_missing_league_shape_refuses_to_price():
    rows = [{"player_id": "x", "name": "X", "position": "RB", "vor": 10.0}]
    out = keepers.analyse(rows, rows, {}, slot=1, draft_rounds=None)
    assert out[0]["surplus"] is None


# ----------------------------------------------------------- null-safe inputs


def test_null_vor_survives_faab():
    bid = analysis.faab_bid({"vor": None, "position": "WR", "name": "x"}, 100, {})
    assert bid["suggested_bid"] >= 1


def test_null_projection_survives_lineup_optimisation():
    result = analysis.optimal_lineup(
        [{"player_id": "a", "name": "A", "position": "QB", "points": None}], ["QB"]
    )
    assert result["projected_total"] == 0.0


def test_construction_handles_empty_and_null_rosters():
    assert construction.find_stacks([]) == []
    assert construction.team_concentration([]) == []
    null_row = {"player_id": "x", "name": None, "position": None, "team": None}
    assert construction.find_stacks([null_row]) == []


# ----------------------------------------------------------------------- news


def test_news_validation_discards_junk_and_unknown_players():
    rows = [{"player_id": "real", "name": "Real Player", "position": "WR",
             "team": "AAA", "points": 100.0, "vor": 10.0}]

    def finder(name, _rows):
        return rows[0] if name == "Real Player" else None

    signals = [
        None,
        "notadict",
        {"player_name": None},
        {"player_name": "Ghost", "signal_type": "injury"},
        {"player_name": "Real Player", "signal_type": "not_a_type"},
        {"player_name": "Real Player", "signal_type": "injury",
         "direction": "negative", "confidence": "high",
         "summary": "s", "evidence": "e"},
    ]
    out = news.validate_signals(signals, rows, finder)
    assert len(out["signals"]) == 1
    assert out["signals"][0]["player"] == "Real Player"


def test_signal_schema_exposes_no_numeric_field():
    """A model must not be able to emit something that looks like a projection."""
    props = news.SIGNAL_SCHEMA["properties"]["signals"]["items"]["properties"]
    assert not [k for k, v in props.items() if v.get("type") in ("number", "integer")]


def test_news_layer_reports_rather_than_raising_without_a_key():
    ok, reason = news.available()
    assert isinstance(ok, bool)
    if not ok:
        assert reason


# ---------------------------------------------------------------------- usage


def test_share_is_per_game_not_per_season():
    """A four-game player must not read as peripheral on season totals.

    This function was previously a closure defined inside the per-player loop,
    capturing `games` from the enclosing scope. Hoisting it to module level
    broke a call site that the network-dependent code path exercised but the
    offline tests did not - hence this test.
    """
    # Both players see 10 targets a game; one played all 17, the other only 4.
    # Per-game normalisation must rate them identically.
    full_season = usage._share(170, 580, 17)
    quarter_season = usage._share(40, 580, 4)
    assert full_season == pytest.approx(quarter_season)

    # A naive season-total share would rate the four-game player at a quarter
    # of the other, which is the bug this function exists to avoid.
    assert (40 / 580) < (170 / 580)


def test_share_returns_none_on_missing_inputs():
    assert usage._share(None, 580, 17) is None
    assert usage._share(100, None, 17) is None
    assert usage._share(100, 580, 0) is None
    assert usage._share(100, 580, None) is None


def test_racr_undefined_on_non_positive_air_yards():
    """Backs and quarterbacks post negative completed air yards."""
    assert usage._racr(378, -56) is None
    assert usage._racr(100, 0) is None
    assert usage._racr(150, 100) == pytest.approx(1.5)


def test_opportunity_flags_handle_missing_data():
    assert usage.opportunity_flags(None, "WR")
    assert usage.opportunity_flags({}, "DL")


# --- roster-aware value: you can only start so many of a position -----------

ONE_TE_ROSTER = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "BN", "BN", "BN"]


def _p(name, pos, pts, vor):
    return {"player_id": name, "name": name, "position": pos, "points": pts, "vor": vor}


def _filled_roster():
    """Starters all filled, both flex spots taken by a decent RB and WR."""
    return [
        _p("QB1", "QB", 300, 50), _p("RB1", "RB", 260, 110),
        _p("RB2", "RB", 230, 80), _p("WR1", "WR", 250, 95),
        _p("WR2", "WR", 225, 70), _p("TE1", "TE", 200, 80),
        _p("RB3", "RB", 210, 60), _p("WR3", "WR", 205, 50),
    ]


def test_backup_te_adds_nothing_to_a_full_lineup():
    """A second TE cannot start once TE and both flex spots are better filled."""
    owned = _filled_roster()
    assert analysis.marginal_value(_p("TE4", "TE", 170, 60), owned, ONE_TE_ROSTER) == 0.0


def test_te_who_beats_a_starter_does_add_value():
    """The lineup check must not blanket-zero every TE - only the unplayable ones."""
    owned = _filled_roster()
    elite = _p("TEx", "TE", 260, 130)
    assert analysis.marginal_value(elite, owned, ONE_TE_ROSTER) > 0


def test_hoarded_te_ranks_below_a_receiver_with_less_vor():
    """The reported bug: naive VOR keeps rating TE4 over WR5 in a 1-TE league."""
    owned = _filled_roster() + [_p("TEa", "TE", 150, 30), _p("TEb", "TE", 140, 25)]
    te4 = analysis.effective_value(_p("TE4", "TE", 170, 60), owned, ONE_TE_ROSTER)
    wr5 = analysis.effective_value(_p("WR5", "WR", 185, 45), owned, ONE_TE_ROSTER)
    assert te4["vor"] > wr5["vor"], "precondition: raw VOR prefers the TE"
    assert wr5["effective_value"] > te4["effective_value"]


def test_first_backup_at_a_position_keeps_its_value():
    """Depth is not worthless: starters miss time, so TE2 is not discounted."""
    owned = _filled_roster()
    assert analysis.effective_value(
        _p("TE4", "TE", 170, 60), owned, ONE_TE_ROSTER
    )["depth_discount"] == 1.0


def test_bench_floor_never_rewards_a_below_replacement_player():
    owned = _filled_roster()
    assert analysis.effective_value(
        _p("bad", "WR", 40, -60), owned, ONE_TE_ROSTER
    )["bench_floor"] == 0.0


def test_need_tilt_never_flips_sign_on_negative_value():
    """Scaling a signed number inverts the intent below zero.

    A 0.88 'penalty' on -60 returns -52.8, which promotes the player. The tilt
    must apply to the upside only.
    """
    for urgency in ("critical", "thin", "adequate", "surplus"):
        assert analysis.apply_need(-60.0, urgency) == -60.0
        assert analysis.apply_need(0.0, urgency) == 0.0
    assert analysis.apply_need(60.0, "critical") > 60.0
    assert analysis.apply_need(60.0, "surplus") < 60.0


DEF_ROSTER = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "DEF", "BN", "BN", "BN"]


def test_backup_defence_is_worth_far_less_than_a_backup_rb():
    """A team defence never misses a game, so a second one is near-worthless.

    A flat bench share floored DEF2 high enough to outrank real players, which
    made a full-draft sim take five of them.
    """
    assert analysis.bench_share("DEF") < analysis.bench_share("RB") / 3
    assert analysis.bench_share("K") == analysis.bench_share("DEF")


def test_qb_carries_the_highest_bench_share():
    """Benchings count: a benched starter plays the backup just as injury does."""
    assert analysis.bench_share("QB") == max(analysis.BENCH_SHARE_BY_POS.values())


def test_unknown_position_falls_back_rather_than_zeroing():
    assert analysis.bench_share("LB") == analysis.BENCH_SHARE_DEFAULT
    assert analysis.bench_share(None) == analysis.BENCH_SHARE_DEFAULT


def test_second_defence_does_not_outrank_a_startable_player():
    owned = _filled_roster() + [_p("D1", "DEF", 120, 19.0)]
    d2 = analysis.effective_value(_p("D2", "DEF", 117, 17.0), owned, DEF_ROSTER)
    rb = analysis.effective_value(_p("RB9", "RB", 150, 25.0), owned, DEF_ROSTER)
    assert rb["effective_value"] > d2["effective_value"]


def test_form_windows_are_always_equal_length():
    """Unequal halves make the prior side noisier and every delta unstable."""
    for total in range(4, 20):
        games = list(range(total))
        eff = min(4, len(games) // 2)
        recent, prior = games[-eff:], games[-2 * eff : -eff]
        assert len(recent) == len(prior), total
        assert not set(recent) & set(prior), total
        assert eff <= 4


def test_unstartable_position_is_never_recommended():
    """A FB cannot fill any slot in a RB/WR/TE flex league, so he is not a pick."""
    avail = [_p("FB1", "FB", 90, 0.0), _p("WRz", "WR", 150, -20.0)]
    recs = analysis.draft_recommendations(avail, _filled_roster(), ONE_TE_ROSTER, 12, 150)
    assert all(r["position"] != "FB" for r in recs)


def test_streamable_positions_are_capped_at_what_you_start():
    """Only ~12 defences are drafted, so DEF keeps positive VOR after every
    skill position is exhausted - and would otherwise win every late pick."""
    owned = _filled_roster() + [_p("D1", "DEF", 120, 19.0)]
    avail = [_p("D2", "DEF", 117, 17.0), _p("WRz", "WR", 150, -20.0)]
    recs = analysis.draft_recommendations(avail, owned, DEF_ROSTER, 12, 150)
    assert all(r["position"] != "DEF" for r in recs)


def test_below_replacement_ranks_on_depth_not_vor_magnitude():
    """TE replacement is shallow, so a late TE's VOR looks better than a WR's
    for reasons that have nothing to do with either being playable."""
    owned = _filled_roster() + [_p("TEb", "TE", 150, 30.0)]
    te = analysis.effective_value(_p("TEz", "TE", 140, -5.0), owned, ONE_TE_ROSTER)
    wr = analysis.effective_value(_p("WRz", "WR", 130, -25.0), owned, ONE_TE_ROSTER)
    assert te["vor"] > wr["vor"], "precondition: raw VOR prefers the TE"
    assert wr["effective_value"] > te["effective_value"]


def test_below_replacement_players_do_not_all_tie_at_zero():
    """max(marginal, -surplus) would return 0.0 for every bench player."""
    owned = _filled_roster() + [_p("TEb", "TE", 150, 30.0)]
    scores = {
        pos: analysis.effective_value(_p("x", pos, 100, -10.0), owned, ONE_TE_ROSTER)[
            "effective_value"
        ]
        for pos in ("TE", "WR", "RB")
    }
    assert len(set(scores.values())) > 1, scores
