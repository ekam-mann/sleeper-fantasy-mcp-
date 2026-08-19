"""Turning football news prose into structured signals.

This is the one place in the project where a language model is used, and the
boundary is deliberate.

Everything else here is deterministic: VOR, xFP, tiers, Monte Carlo. Those are
provable, and `validate.py` asserts properties about them. You cannot write an
invariant check against generated text, so the moment a model produces a number
that lands in a ranking, the ranking stops being verifiable.

So the model is confined to the job it is actually better at than code:
reading prose and saying what it contains. Coaching changes, role shifts,
contract situations and beat-writer intel live in sentences, not in an API
field, which is why they were the long-standing gap in this tool.

The rules this module holds itself to:

  1. It only ever emits *categorical* fields and short quotes. No projections,
     no point totals, no rankings, no weights.
  2. Every signal is validated after generation, and a signal naming a player
     who cannot be resolved against the league's own player list is discarded.
  3. Output is labelled as unverified and kept separate from measured metrics,
     so a reader never mistakes an inference for a number.
  4. It is entirely optional. With no API key, or without the `anthropic`
     package installed, the tool reports that plainly and everything else in
     the project continues to work.

There are two ways to run it, and the keyless one is the default:

  **Delegated (no API key).** This is an MCP server, so the thing calling it is
  already a language model. Rather than paying for a second one, the tool hands
  back the article text, the schema, and the extraction rules, and the calling
  model does the reading. Its output then comes back through `validate_signals`
  and faces exactly the same checks. No key, no extra cost, same guarantees.

  **Direct (API key).** The server calls Claude itself. Useful when this runs
  headless - a cron job, a script, anything with no model already attached.

The validation boundary is identical either way, which is the point: the
safety of this feature comes from what happens *after* generation, not from
which model did the generating.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import secrets

ESPN_NEWS = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"

MODEL = "claude-opus-5"

SIGNAL_TYPES = [
    "role_change",
    "injury",
    "depth_chart",
    "scheme_change",
    "holdout_or_contract",
    "suspension",
    "coaching_change",
    "other",
]

# The response schema. Deliberately free of any numeric field - the model is
# not permitted to produce a quantity that could be mistaken for a projection.
SIGNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "player_name": {
                        "type": "string",
                        "description": "Full name exactly as written in the article.",
                    },
                    "team": {
                        "type": "string",
                        "description": "NFL team abbreviation, or empty if not stated.",
                    },
                    "signal_type": {"type": "string", "enum": SIGNAL_TYPES},
                    "direction": {
                        "type": "string",
                        "enum": ["positive", "negative", "neutral"],
                        "description": "Effect on this player's fantasy outlook.",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": (
                            "high = explicitly stated as fact; medium = strongly "
                            "implied; low = speculation or a reporter's opinion."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": "One sentence, factual, no projection of points.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "A short verbatim quote from the source text.",
                    },
                },
                "required": [
                    "player_name",
                    "team",
                    "signal_type",
                    "direction",
                    "confidence",
                    "summary",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["signals"],
    "additionalProperties": False,
}

SYSTEM = """You extract structured fantasy-football signals from news text.

Report only what the text states or clearly implies. Do not use outside
knowledge about players, and do not speculate beyond the text.

Never estimate fantasy points, projections, rankings, or any other number. Your
job is to say what happened and who it affects, not what it is worth - the
surrounding system computes value itself.

If the text contains no concrete signal about a specific player, return an
empty list. An empty result is a correct answer and is much more useful than an
invented one."""


def available() -> tuple[bool, str | None]:
    """Whether signal extraction can run, and why not if it cannot."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, (
            "The `anthropic` package is not installed. Run "
            "`pip install anthropic` to enable news signal extraction."
        )
    if not secrets.anthropic_key():
        return False, (
            "No Anthropic API key configured. Set ANTHROPIC_API_KEY or add "
            "`anthropic_api_key` to secrets.json to enable news signals."
        )
    return True, None


def fetch_headlines(limit: int = 20) -> list[dict]:
    """Recent NFL news from ESPN's public feed."""
    try:
        resp = httpx.get(
            ESPN_NEWS, params={"limit": limit}, timeout=20.0, follow_redirects=True
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    out = []
    for a in data.get("articles") or []:
        out.append(
            {
                "headline": a.get("headline"),
                "description": a.get("description"),
                "published": a.get("published"),
                # ESPN tags player and team names as categories - useful for
                # sanity-checking what the model claims the article is about.
                "tagged": [
                    c.get("description")
                    for c in (a.get("categories") or [])
                    if c.get("type") == "athlete" and c.get("description")
                ],
            }
        )
    return out


def _as_text(articles: list[dict]) -> str:
    chunks = []
    for i, a in enumerate(articles, start=1):
        parts = [f"[{i}] {a.get('headline') or ''}"]
        if a.get("description"):
            parts.append(a["description"])
        chunks.append("\n".join(parts))
    return "\n\n".join(chunks)


def extract_signals(articles: list[dict], runs_note: str = "") -> dict:
    """Ask Claude to turn article text into validated structured signals."""
    ok, reason = available()
    if not ok:
        return {"error": reason, "signals": []}

    import anthropic

    if not articles:
        return {"signals": [], "note": "No articles to read."}

    client = anthropic.Anthropic(api_key=secrets.anthropic_key())

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            # Opus 5 safety classifiers can decline a request; a server-side
            # fallback re-runs it on another model rather than returning a
            # refusal we would have to handle as an outage.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={
                "effort": "low",  # extraction, not reasoning - keep it cheap
                "format": {"type": "json_schema", "schema": SIGNAL_SCHEMA},
            },
            system=SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"Extract signals from these items.\n\n{_as_text(articles)}",
                }
            ],
        )
    except Exception as e:  # network, auth, rate limit - never fatal
        return {"error": f"Signal extraction failed: {type(e).__name__}: {e}", "signals": []}

    # A refusal returns HTTP 200 with an empty or partial body, so check the
    # stop reason before touching content.
    if response.stop_reason == "refusal":
        return {
            "error": "The model declined to process this text.",
            "signals": [],
        }

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return {"error": "No content returned.", "signals": []}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"error": "Model returned malformed JSON.", "signals": []}

    return {"signals": parsed.get("signals") or [], "note": runs_note or None}


def validate_signals(signals: list[dict], rows: list[dict], finder) -> dict:
    """Discard anything that cannot be tied to a real player in this league.

    The model is not trusted to be the last word on who exists. A name that
    does not resolve against the league's own player list is dropped rather
    than surfaced - a signal about a player nobody can roster is noise at best
    and a hallucination at worst.
    """
    kept: list[dict] = []
    dropped: list[dict] = []

    for s in signals:
        if not isinstance(s, dict):
            continue
        name = (s.get("player_name") or "").strip()
        if not name:
            continue

        if s.get("signal_type") not in SIGNAL_TYPES:
            dropped.append({"name": name, "reason": "unknown signal type"})
            continue

        match = finder(name, rows)
        if not match:
            dropped.append({"name": name, "reason": "no matching player in this league"})
            continue

        kept.append(
            {
                "player": match["name"],
                "player_id": match["player_id"],
                "position": match["position"],
                "team": match.get("team"),
                "signal_type": s.get("signal_type"),
                "direction": s.get("direction"),
                "confidence": s.get("confidence"),
                "summary": s.get("summary"),
                "evidence": s.get("evidence"),
                # Carried so a reader can weigh the signal against what the
                # deterministic side already knows about the player.
                "current_projection": match.get("points"),
                "current_vor": match.get("vor"),
                "adp": match.get("adp"),
            }
        )

    order = {"high": 0, "medium": 1, "low": 2}
    kept.sort(key=lambda s: order.get(s.get("confidence"), 9))
    return {"signals": kept, "discarded": dropped}


def extraction_brief(articles: list[dict]) -> dict:
    """Everything a calling model needs to extract signals itself.

    The keyless path. An MCP client is already a language model, so instead of
    the server making its own API call, it returns the text and the contract
    and lets the caller do the reading. Whatever comes back still has to pass
    `validate_signals`, so the trust boundary does not move.
    """
    return {
        "task": (
            "Read the articles below and extract structured fantasy-football "
            "signals. Then call `submit_news_signals` with your result to have "
            "it validated against this league's player list."
        ),
        "rules": SYSTEM,
        "schema": SIGNAL_SCHEMA,
        "articles": articles,
        "reminder": (
            "Report only what the text says. Never output a projection, "
            "ranking, or any other number - the surrounding system computes "
            "value itself. An empty list is a valid and useful answer."
        ),
    }
