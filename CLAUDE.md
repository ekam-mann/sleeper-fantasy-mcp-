# Working rules for this project

## The core rule: question every decision, then justify it

Do not make a choice and move on. For every decision — a formula, a threshold, a
field, a default, a filter, a ranking — state what you chose, why, and what would
have to be true for it to be wrong. If you cannot justify it, you have not
finished thinking about it.

This applies to decisions that feel obvious. The expensive mistakes in this
project all looked obvious at the time.

Concretely, before moving past a decision, ask:

- **What does this field actually contain?** Not what its name suggests.
- **What would this look like if I were wrong?** Then go check for that.
- **Is this result plausible?** If a number surprises you, chase it before
  accepting it.
- **Does this hold for every configured league?** They have different rules.
- **What am I assuming that I have not verified?**

Then say the answer out loud in the response. The reasoning is part of the
deliverable, not scaffolding to be discarded.

## Verify data semantics before modelling on a field

Every serious bug here came from trusting a field name.

- `rec_air_yd` looked like intended air yards. It is *completed* air yards —
  proven by `rec_air_yd + rec_yar == rec_yd` exactly. True aDOT is not derivable
  and the code says so rather than shipping a wrong number under a right name.
- `rec_0_4`, `rec_10_19` etc. looked like target-depth buckets, which would have
  been ideal xFP inputs. They sum to *receptions* — they are outcomes, not
  opportunities, and would have leaked the answer into the model.
- Sleeper omits `pts_ppr` entirely for a player who dressed but scored nothing,
  rather than writing 0. That silently dropped 2,021 real zero-point weeks and
  inflated the floor of every low-usage player.
- Season stats return rows for seasons *before* a player entered the league, with
  0 games. Uncorrected, every rookie looks maximally injury-prone.

The pattern: check that a field means what you think by testing an identity that
must hold if it does.

## Sanity-check outputs, and chase what looks off

A surprising number is a lead, not a nuisance.

- Implied totals not summing to the game total → turned out to be rounding, but
  only after checking.
- Negative RACR for 94 players → RBs post negative air yards; the metric does not
  apply to them.
- QBs showing the *highest* missed-game rate → real, and it is benchings, not
  injuries. That is why the module is called `availability`, not `injury`.
- Backup QBs topping the streaming list → they inherit a matchup on paper and
  will not take a snap.

## Never let a model output something impossible

Constrain the maths to reality rather than reporting a nonsense number:

- Opportunity weights in `xfp` are constrained non-negative. A red-zone carry
  cannot have negative expected value; plain OLS will happily say it does.
- Share metrics are per-game, not per-season, or a four-game alpha reads as a
  decoy.
- Metrics defined only for some positions are computed only for those positions.

## Everything must be league-scored

Leagues differ, and those differences change every number:

| | Example league A | Example league B |
|---|---|---|
| Teams | 12 | 8 |
| QB | 1QB | **SUPERFLEX** |
| Kicker | none | yes |
| Waivers | reverse-standings priority | **FAAB** |

If both configured leagues happen to use identical scoring, raw `pts_ppr` will
*happen* to be correct. That is a coincidence, not a guarantee — pass
`scoring_settings` through every historical calculation so correctness survives
a settings change. Player facts (usage, availability, Vegas, weather) are
deliberately league-agnostic; anything expressed in points is not.

Same player, opposite conclusions: an elite QB is a defensible round-2 pick in
superflex and a clear mistake in 1QB. Never answer "is this a good pick"
without naming the league.

**Keepers.** A league may carry a non-zero `max_keepers` it does not actually
use, so `configured` and `in use` are reported separately. Keeper value is
surplus over the pick forfeited - never raw player value.

## Prove claims, do not assert them

- Run `python validate.py` after touching any model. It asserts invariants
  across every model; they
  assert correctness, not that nothing threw. Two bugs slipped past smoke tests
  and were caught here.
- When comparing strategies, simulate rather than reasoning from one path. A
  single-path comparison credited a player who was only 57% likely to be there;
  a 3,000-draft simulation gave the opposite answer.
- Show the numbers that drove the conclusion.

## Say what is not built

Distinguish "done", "computable but not built", and "blocked by data access".
Never imply completeness that does not exist. Currently blocked without a paid
source: PFF grades, Next Gen Stats, true aDOT, EPA/DVOA, O-line quality.

## Practical notes

- Secrets go in `secrets.json` (gitignored) or env vars, never in source.
- `.league_state.json` sits outside `.cache/` on purpose — `refresh_data` wipes
  the cache, which would destroy the watchdog's comparison baseline.
- Heredocs with non-ASCII break in this shell; write a patch script instead.
- Assertions in patch scripts run *before* the write. A failed assert on the last
  substitution silently discards the earlier ones.
