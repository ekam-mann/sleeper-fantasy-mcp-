# sleeper-fantasy-mcp

An MCP server that turns [Sleeper](https://sleeper.app) league data into
league-accurate fantasy football advice. Every projection is scored through
*your* league's actual scoring settings, and value is expressed as VOR against
replacement given your roster shape — so a superflex league and a 1QB league
get genuinely different answers, not the same ranking with a different label.

Read-only. Uses public, unauthenticated APIs (Sleeper, ESPN) plus an optional
free OpenWeather key.

It answers the questions you actually ask mid-draft or mid-week: who to take, whether
a trade is good, who to pick up, who to start.

## What makes it league-accurate

Most fantasy tools rank players in generic PPR. This one doesn't.

Sleeper exposes each league's `scoring_settings` using the **same stat keys** that
appear in projected stat lines. So every projection is re-scored as a dot product
against your league's actual rules, then converted to **VOR** (value over
replacement) using your league's actual roster shape.

Replacement level is computed from **your** league — team count, starting slots,
and how flex spots distribute across positions — so the scarcity premium at each
position is whatever your rules actually produce. Nothing about league size or
roster shape is assumed or hardcoded; every value is derived from what Sleeper
returns for the league IDs in your `config.json`. Two leagues with different
settings get different answers from the same player pool, and the same league
gets different answers if you change its settings.

## Setup

Requires **Python 3.10+**.

```bash
git clone https://github.com/<your-username>/sleeper-fantasy-mcp.git
cd sleeper-fantasy-mcp
pip install -r requirements.txt
cp config.example.json config.json
```

Then edit `config.json` with your Sleeper details:

```json
{
  "username": "your_sleeper_username",
  "default_league_id": "000000000000000000",
  "leagues": [
    { "name": "main",   "league_id": "000000000000000000" },
    { "name": "second", "league_id": "111111111111111111" }
  ]
}
```

**Finding your league ID:** open your league on sleeper.app in a browser. The URL
looks like `https://sleeper.app/leagues/1234567890123456789/team` — the long
number is the league ID. You can list as many leagues as you like; every tool
takes a `league_id` argument and falls back to `default_league_id`.

No Sleeper API key is needed — the endpoints used are public and unauthenticated.

**Optional — weather.** The `game_weather` tool needs a free
[OpenWeather](https://openweathermap.org/api) key. Either export it:

```bash
export OPENWEATHER_API_KEY=your_key_here
```

or `cp secrets.example.json secrets.json` and put it there. Both files are
gitignored. A newly created OpenWeather key can take up to a couple of hours to
activate; until it does the tool reports a clear 401 rather than failing oddly.

Run the server:

```bash
python server.py
```

Then point your MCP client at it. To check everything works against your own
leagues:

```bash
python validate.py
```

## Tools

**League**
- `list_leagues` — configured leagues + current NFL week
- `league_info` — roster shape, scoring, waivers, playoffs
- `standings` — record, PF/PA, FAAB remaining
- `my_roster` / `roster` — any team, scored for this league

**Draft**
- `who_should_i_draft` — the main one. Blends VOR, your positional needs, and
  ADP value, and warns on tier cliffs. Call it every time you're on the clock.
- `position_tiers` — where the real value drop-offs fall at a position
- `keeper_analysis` — which players are worth the pick they cost
- `draft_plan` — who will realistically be there at each of YOUR picks
- `draft_board` — best available by VOR, with ADP alongside
- `draft_results` — picks so far, with value-vs-ADP on each

**In-season**
- `waiver_targets` — best free agents + suggested FAAB bid
- `evaluate_trade` — pass names, get a verdict and roster-fit analysis
- `set_lineup` — optimal legal starting lineup, with context flags and close calls
- `matchup_preview` — both lineups, projected margin
- `player` — projection, VOR, rank, owner, tier, prior-season usage, schedule,
  and this week's Vegas game environment
- `vegas_lines` — spreads, totals, implied team totals, game-script reads
- `consistency_report` — floor/ceiling/boom/bust, sortable for your format
- `regression_candidates` — who outran their opportunity (fade) or fell short of it (buy)
- `handcuff_report` — which of your RBs need their backup rostered
- `streaming_options` — best streaming plays at DEF, K, QB or TE
- `roster_risk` — QB/receiver stacks and single-team over-exposure
- `ir_stash_targets` — injured players worth an IR slot
- `waiver_strategy` — FAAB or priority guidance, whichever your league uses
- `simulate_season` — Monte Carlo floor/median/ceiling, one player or several
- `durability_report` — availability history vs positional base rates
- `game_weather` — kickoff forecast + fantasy impact (needs an OpenWeather key)
- `news_signals` / `submit_news_signals` — structured signals read out of news prose
- `trending_players` — league-wide adds/drops across all of Sleeper
- `settings_check` — live scoring/roster settings for every league, plus any drift
- `refresh_data` — clear cache (use mid-draft or after a waiver run)

## How the advice is built

- **VOR** — projected points minus the last startable player at that position.
  Flex slots are distributed across RB/WR/TE so a 2-flex league correctly pushes
  replacement level deeper than a 0-flex one.
- **Need weighting** — a position you have nothing at gets a 1.15x bump; a
  position you're already deep at gets 0.88x. Enough to break ties, not enough to
  make you reach two rounds early.
- **ADP delta** — flags players falling past ADP (bargain) or reaches.
- **FAAB bids** — anchored on VOR as a percentage of *remaining* budget, then
  adjusted for season timing (an early hit plays fifteen more games than a late
  one, and unspent budget is wasted) and for leverage against rivals' remaining
  budgets. Returned as a range, not a false-precision single number.
- **Handcuffs** — RB only, because RB value is volume-driven and volume transfers
  to the next man up. Priority keys off the starter's carry share: a workhorse
  leaves a big transferable role, a committee back leaves little.
- **Lineup context** — the optimiser picks on projected points alone, deliberately:
  silently reordering a lineup because of a weather reading would make the output
  unverifiable. Instead the models attach as **flags** (injury, weather severity,
  implied team total, boom/bust profile, and news signals — the last marked
  unverified, since they were read from prose rather than measured), and are paired with **close calls** — the
  slots where a bench player is within 2.5 projected points. A flag on someone 20
  points clear of his backup is noise; a flag on a half-point start/sit is the
  entire decision.
- **Streaming** — the signal inverts by position. A defense wants its *opponent*
  implied low; an offensive player wants his *own* team implied high plus a soft
  positional matchup. QB and K are filtered to depth-chart starters, or the list
  fills with backups who have a great matchup and will not play.
- **xFP (expected fantasy points)** — what a league-average player would have
  scored with the same opportunities. Weights are *fitted* per position against
  your league's scoring, constrained non-negative, using only true opportunity
  (targets, carries, and their red-zone subsets). Sleeper's `rec_0_4`-style
  buckets are excluded on purpose: they count receptions by yards gained, so they
  are outcomes, not opportunities, and would leak the answer into the model.
  Opportunity is sticky year to year; efficiency and TD luck are not — so a large
  gap either way is a regression signal.
- **Monte Carlo** — samples games played from a player's availability history and
  weekly scores from a gamma calibrated to his volatility, then reports p10/median/p90.
  Gamma because scores are non-negative and right-skewed: there are huge weeks but
  no symmetric huge-negative counterpart.
- **Correlation** — a QB and his own receiver rise and fall together. That's an
  asset in best ball and a liability in weekly head-to-head, where it widens your
  variance without raising your expected score. Always a tiebreaker, never a veto.
- **Waiver priority** — unlike FAAB, priority is all-or-nothing; you can't bid half
  of it. So timing dominates, and the advice keys off your position in the order
  and how much season is left.
- **Tiers** — positions are split where the value gap between consecutive players
  is unusually large *for that position*. No fixed thresholds: the gap that matters
  at QB is a different size from the one at TE.
- **Usage** — prior-season snap/target/carry/red-zone share from Sleeper's stats
  feed, because a projection alone can't tell you whether the role is real.
- **Strength of schedule** — actual PPR points allowed by each defense to each
  position, joined to the upcoming schedule. Weeks 15–17 are reported separately,
  since that's when the league is decided.
- **Implied team totals** — `total/2 ± spread/2`. The best single read on how big
  a scoring pie an offense is walking into.
- **Consistency** — floor (p10), ceiling (p90) and boom/bust rates from last
  season's weekly scores. Boom/bust lines are per-position percentiles of the
  startable pool, not fixed numbers, because 18 points means something different
  at TE than at RB. Sort by floor for weekly head-to-head, ceiling for best ball.
- **Availability** — games played over the 17-game era, anchored on each player's
  debut so rookies aren't charged for seasons before they entered the league, and
  compared against measured positional base rates.

## News signals — the one model-backed layer

Everything above is deterministic. `news_signals` is the exception, and it's fenced:

- It emits **categorical fields and quotes only** — the schema has no numeric field,
  so a model can't produce something that looks like a projection.
- Every signal is checked against the league's real player list; anything that
  doesn't resolve is **discarded**, not shown.
- Output is labelled unverified and kept separate from measured metrics.

**It needs no API key by default.** This is an MCP server, so the caller is already
a language model — `news_signals` hands back the article text plus the schema, the
caller extracts, and `submit_news_signals` runs the same validation on the result.
Set `mode="direct"` (with `anthropic` installed and a key) to have the server call
Claude itself instead, for headless use. The validation boundary is identical either
way, which is the point: the safety comes from what happens after generation, not
from which model generated it.

## Validation

`python validate.py` runs ~113 invariant checks across every model (a few are cross-league comparisons, so the exact count depends on how many leagues you configure) — percentile
ordering, VOR reconciling against replacement, implied totals summing to the game
total, non-negative xFP weights, snake pick arithmetic, and league scoring actually
propagating. These assert correctness, not just that nothing threw.

## Settings watchdog

Every value in this tool is downstream of scoring settings and roster shape. Change
either and replacement levels move, VOR moves, tiers move — and nothing errors, the
advice just quietly stops matching the league.

So **every command re-reads every league's settings** and compares them against a
stored baseline (`.league_state.json`, gitignored). Any drift is attached to that
command's output as `settings_alert`, naming the exact fields that moved.

Three design notes:

- The check **bypasses the cache** — a cached copy of the settings cannot tell you
  the settings changed.
- League fetches run **in parallel**, so the cost is one round trip (~350ms) rather
  than one per league.
- It is **fail-safe**: any error verifying settings is swallowed and the command
  still returns. A watchdog that can break the thing it watches is worse than none.

## Caching

Responses cache to `.cache/` — 24h for the player dump (~5MB), 6h for projections,
5min for rosters, 30s for live draft picks. Sleeper asks callers to stay under
1000 requests/minute; this stays far below that. Call `refresh_data` to force-clear.

## Bye weeks

Sleeper publishes none — the `bye_week` field is absent from every player in their
dump. So byes come from ESPN's public scoreboard API instead, which exposes an
explicit `teamsOnBye` list per week. No auth, one request per week, cached for a
week. `ff/schedule.py` handles it.

Byes are read live per season, so there is nothing season-specific hardcoded here.

`who_should_i_draft` warns when a pick would give you 3+ players on the same bye,
and `my_roster` reports `bye_conflicts` for any week you'd have multiple players out.

## Configuration

Leagues live in `config.json`. API keys live in `secrets.json` (gitignored) or
environment variables — env wins:

```json
{ "openweather_api_key": "..." }
```

`OPENWEATHER_API_KEY` is the env equivalent. Weather is the only feature needing
a key; everything else runs on public, unauthenticated endpoints.

## Keepers

Behaviour follows the league's own settings. `max_keepers: 0` means keepers are off
and `keeper_analysis` says so rather than inventing advice; above zero it prices
each candidate. `league_info` reports **configured** and **in use** separately,
because a league can carry a non-zero `max_keepers` it never actually uses, and
advising on a mechanic nobody plays with is worse than staying quiet.

A keeper is never free — keeping someone forfeits a draft pick, so the question is
whether he beats whoever that pick would have returned. That difference is his
**surplus**, and it's what the tool ranks on:

- An elite player at an expensive cost can be a *bad* keeper. Keeping the consensus
  1.02 at a first-round price buys nothing you couldn't have had by drafting him.
- A useful starter at a cheap cost is often a *great* one — most of a free roster spot.

Sleeper stores that keepers exist (`max_keepers`, `roster.keepers`, `is_keeper` on
picks) but **not what they cost**, so the pricing rule is a parameter rather than a
guess: `same_round` (default), `round_earlier`, or `two_rounds_earlier`, against the
round the player was drafted the previous season. Pick value comes from ADP — what
the market actually returns at that slot — floored at zero, since a pick returning a
below-replacement player is worth nothing rather than a negative.

## Known limitations

- **Projections are Sleeper's.** They're reasonable but they're one source. The
  value model is the differentiator here, not the underlying projections.
- **Read-only.** Sleeper's API cannot set lineups or submit waiver claims. This
  tells you what to do; you still tap the buttons in the app.
- The projections/ADP host is undocumented. It's stable in practice but Sleeper
  makes no promises about it.
- **No true aDOT.** Sleeper publishes *completed* air yards, not intended air
  yards on all targets (verified: `rec_air_yd + rec_yar == rec_yd` exactly). So
  `depth_per_catch` and `yac_per_catch` are reported instead — real aDOT would
  need a paid source.
- **Availability is not injury.** Games played mixes injury with benchings and
  rest, and the two cannot be separated from this feed. The measured base rates
  show it plainly: QB has the *highest* missed-game rate (6.31/season vs RB's
  4.92), which is backups being benched, not quarterbacks being hurt. Read the
  number as availability, and weigh it only for players with a real role.
- **Consistency is backward-looking.** It describes last season's weekly shape,
  not next season's. A changed role, offense or depth chart invalidates it.
- **The simulation is a model, not a forecast.** It inherits every bias in the
  underlying Sleeper projection and only adds the shape that projection omits. It
  does not know about holdouts, scheme changes or camp news.
- **No kicker matchup data.** Points-allowed splits don't cover kickers, so kicker
  streaming keys purely off the team's scoring environment.
- **Weather reaches ~5 days out.** It's a lineup tool, not a draft tool. Dome and
  retractable-roof games are filtered out rather than given a meaningless reading.
- **Weather severity thresholds are priors, not fitted.** The wind/snow/rain cutoffs
  come from conventional wisdom about how conditions affect passing and kicking, not
  from a regression against outcomes — unlike xFP or the simulation, whose weights are
  derived from data. The banding is asserted (monotonic in wind, gusts weighted, full
  0–3 range) but the *thresholds themselves* are judgement.
- **No PFF/Next Gen Stats** — no O-line grades, yards per route run, or EPA.
  Those need paid or non-public sources.

## Privacy

This project reads **public** Sleeper league data and holds no personal
information about anyone. There is no account system, no analytics, no
telemetry, and nothing is transmitted anywhere except to the APIs listed above.

- Your league IDs live in `config.json` and your API keys in `secrets.json`.
  Both are gitignored and never leave your machine.
- Cached API responses live in `.cache/` — also gitignored. Delete the folder,
  or run `refresh_data`, to clear it at any time.
- The repository itself contains no league data, no rosters, and no sample
  datasets scraped from a real league.

If you publish output from this tool, remember it may contain your leaguemates'
display names as Sleeper returns them.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), the [Code of Conduct](CODE_OF_CONDUCT.md),
and [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Disclaimer

Not affiliated with, endorsed by, or connected to Sleeper, the NFL, ESPN, or
OpenWeather. It reads their public endpoints; you are responsible for complying
with their respective terms of service. Projections come from Sleeper and are
one source among many — the value model is the contribution here, not the
underlying projections.

Licensed under the [MIT License](LICENSE).
