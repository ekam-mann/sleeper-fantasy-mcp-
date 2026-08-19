**What changed and why**

**How it was verified**
- [ ] `pytest tests/ -q` passes
- [ ] `ruff check .` passes
- [ ] `python validate.py` passes against a real league (if a model changed)
- [ ] Added a test or invariant for the new behaviour

**Checklist**
- [ ] No hardcoded league shape, season, or player names
- [ ] Anything in points passes through `scoring_settings`
- [ ] No personal league data in code, tests, or docs
