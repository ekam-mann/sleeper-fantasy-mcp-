"""Credential loading. Keys never live in source.

Order of preference:
  1. environment variable  (best for CI / shared machines)
  2. secrets.json          (gitignored, convenient for local use)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.json"


def get(name: str, env_var: str | None = None) -> str | None:
    if env_var and (v := os.environ.get(env_var)):
        return v
    if SECRETS_PATH.exists():
        try:
            return json.loads(SECRETS_PATH.read_text(encoding="utf-8")).get(name)
        except json.JSONDecodeError:
            return None
    return None


def openweather_key() -> str | None:
    return get("openweather_api_key", "OPENWEATHER_API_KEY")


def anthropic_key() -> str | None:
    """Optional - only the news-signal extractor uses this."""
    return get("anthropic_api_key", "ANTHROPIC_API_KEY")
