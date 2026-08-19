# Contributing

Thanks for taking a look. Contributions are welcome under the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

```bash
git clone https://github.com/<you>/sleeper-fantasy-mcp.git
cd sleeper-fantasy-mcp
pip install -r requirements.txt
pip install pytest ruff
cp config.example.json config.json   # add your own Sleeper league ID
```

## Running the checks

```bash
pytest tests/ -q     # offline: no network, no league needed. CI runs this.
ruff check .         # lint
python validate.py   # full correctness suite (needs a configured league + network)
```

`tests/` covers the pure logic and runs anywhere. `validate.py` asserts
invariants against live data, so it needs a real league in `config.json` — run
it locally before opening a pull request that touches a model.

## The one rule that matters here

**Verify what a field contains before modelling on it.** Every serious bug in
this project came from trusting a field name. Some real examples:

- `rec_air_yd` reads like intended air yards. It is *completed* air yards —
  provable because `rec_air_yd + rec_yar == rec_yd` exactly.
- `settings.draft_rounds` is not the draft length. A 15-round draft reported 3.
- Sleeper omits `pts_ppr` entirely for a player who dressed and scored nothing,
  rather than writing 0.

The habit that catches these: find an identity that must hold if the field
means what you think, and test it.

## Conventions

- **Derive, never assume.** No hardcoded team counts, roster shapes, draft
  lengths, or seasons. Everything comes from the API for the configured league.
- **Nothing impossible.** Constrain the maths rather than reporting nonsense —
  opportunity weights are non-negative, pick values are floored at zero.
- **Score through the league.** Anything expressed in points must pass through
  that league's `scoring_settings`.
- **Say what isn't built.** Distinguish "done", "computable but not built", and
  "blocked by data access". Don't imply completeness that isn't there.
- Add a test with behaviour changes. If it can be tested offline, put it in
  `tests/`; if it needs live data, add an invariant to `validate.py`.

## Pull requests

Fork, branch, and open a PR describing what changed and why. Link any related
issue. CI must pass. Keep unrelated changes in separate PRs — it makes review
and bisecting much easier.

By contributing you agree your work is licensed under the project's
[MIT License](LICENSE).
