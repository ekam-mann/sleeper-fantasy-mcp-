"""Settings drift detection.

Every number this tool produces is downstream of two things: the league's
scoring settings and its roster shape. Change either and replacement levels
move, VOR moves, tiers move, and every ranking silently becomes wrong. Nothing
errors - the advice just quietly stops matching the league.

So we fingerprint the settings that actually feed the model, re-read them on
every command, and shout if anything moved.

Two deliberate design choices:

  - **The fingerprint is fetched fresh, bypassing the cache.** A cached copy of
    the settings cannot tell you the settings changed, which is the entire
    point. It is one small request per league.
  - **The baseline is only advanced once an alert has actually been shown.**
    If a change is detected during a command whose output cannot carry the
    alert, the alert survives to the next command rather than being swallowed.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from . import context

# Deliberately NOT inside .cache/ - refresh_data wipes that directory, and
# wiping the baseline would destroy the very history we are comparing against.
STATE_PATH = Path(__file__).resolve().parent.parent / ".league_state.json"

BASE = "https://api.sleeper.app/v1"

# Settings that change the maths. Cosmetic fields are left out so a league
# rename does not raise a false alarm about your rankings.
WATCHED_SETTINGS = [
    "waiver_type",
    "waiver_budget",
    "trade_deadline",
    "playoff_week_start",
    "playoff_teams",
    "reserve_slots",
    "taxi_slots",
    "num_teams",
]


# A watchdog that blocks the answer is worse than one that occasionally cannot
# verify. This runs on every single command, so a slow or flaky API must cost a
# couple of seconds and a caveat - never the whole call. Measured against a
# degraded network where Sleeper took 40s a request while ESPN took 1.7s.
FETCH_TIMEOUT = 3.0

# How long a clean verification stays good for.
#
# League settings are changed by a commissioner in a web UI - a rare, manual,
# deliberate act - not something that drifts on its own. Re-reading them twice
# per function call priced a constant tax against an event that happens maybe
# once a season, so this is deliberately long.
#
# The cost of the window is bounded and known: a settings change made mid-session
# can go unreported for up to this long. Two escape hatches keep that acceptable
# - `settings_check` forces a real read on demand, and `refresh_data` resets the
# window, so any explicit "re-read the world" gesture gets fresh settings.
#
# Only a CLEAN result is allowed to satisfy the window. If drift was found, the
# alert must keep surfacing until something acknowledges it, so a dirty result
# always forces a real re-read.
GRACE_SECONDS = 30 * 60.0

_LAST: dict[str, object] = {"at": 0.0, "clean": False}


def _fetch_league(league_id: str) -> dict | None:
    """Fresh league read, deliberately uncached.

    Returns None on timeout, which the caller reports as "unverified" rather
    than treating as "unchanged" - failing open on latency, but never silently
    claiming the settings were checked when they were not.
    """
    try:
        resp = httpx.get(f"{BASE}/league/{league_id}", timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _fetch_all(league_ids: list[str]) -> dict[str, dict | None]:
    """Fetch every league at once.

    This runs on every single command, so the round trips are made in parallel
    rather than in series - the cost of the guarantee should be one request's
    latency, not one per league.
    """
    if not league_ids:
        return {}
    if len(league_ids) == 1:
        return {league_ids[0]: _fetch_league(league_ids[0])}

    with ThreadPoolExecutor(max_workers=min(8, len(league_ids))) as pool:
        return dict(zip(league_ids, pool.map(_fetch_league, league_ids)))


def fingerprint(lg: dict) -> dict:
    """The settings that actually feed the value model."""
    settings = lg.get("settings") or {}
    return {
        "name": lg.get("name"),
        "season": lg.get("season"),
        "status": lg.get("status"),
        "total_rosters": lg.get("total_rosters"),
        "roster_positions": lg.get("roster_positions") or [],
        "scoring_settings": lg.get("scoring_settings") or {},
        "settings": {k: settings.get(k) for k in WATCHED_SETTINGS},
    }


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass  # a failed write must never break a command


def _diff(old: Any, new: Any, path: str = "") -> list[str]:
    """Human-readable differences between two fingerprints."""
    changes: list[str] = []

    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            where = f"{path}.{key}" if path else key
            if key not in old:
                changes.append(f"{where}: added ({new[key]!r})")
            elif key not in new:
                changes.append(f"{where}: removed (was {old[key]!r})")
            else:
                changes.extend(_diff(old[key], new[key], where))
        return changes

    if isinstance(old, list) and isinstance(new, list):
        if old != new:
            # Roster shape is the case that matters; show it as a whole.
            changes.append(f"{path}: {old!r} -> {new!r}")
        return changes

    if old != new:
        changes.append(f"{path}: {old!r} -> {new!r}")
    return changes


def check(acknowledge: bool = False, force: bool = False) -> dict | None:
    """Re-read every configured league and report any settings drift.

    Returns None when nothing changed. Pass acknowledge=True once the result
    has actually been surfaced, to advance the stored baseline. Pass force=True
    to bypass the grace window and always hit the network.
    """
    now = time.monotonic()
    if (
        not force
        and _LAST["clean"]
        and (now - float(_LAST["at"])) < GRACE_SECONDS  # type: ignore[arg-type]
    ):
        return None

    try:
        cfg = context.load_config()
    except Exception:
        return None

    leagues = cfg.get("leagues") or []
    state = _load_state()
    alerts: dict[str, list[str]] = {}
    fresh: dict[str, dict] = {}
    unreachable: list[str] = []

    ids = [e["league_id"] for e in leagues if e.get("league_id")]
    fetched = _fetch_all(ids)

    for entry in leagues:
        lid = entry.get("league_id")
        label = entry.get("name") or lid
        if not lid:
            continue

        lg = fetched.get(lid)
        if lg is None:
            unreachable.append(label)
            continue

        fp = fingerprint(lg)
        fresh[lid] = fp
        previous = state.get(lid)

        if previous is None:
            continue  # first sighting: record it, do not alarm

        changes = _diff(previous, fp)
        if changes:
            alerts[label] = changes

    if acknowledge or not alerts:
        # Record what we saw. On a clean run this is just bookkeeping; on an
        # acknowledged run it accepts the new settings as the baseline.
        merged = {**state, **fresh}
        if merged != state:
            _save_state(merged)

    if not alerts and not unreachable:
        # Fully verified and unchanged - this is the only outcome the grace
        # window may serve. An unreachable league is NOT clean: it means we did
        # not actually verify, so the next call must try again rather than
        # inheriting a silence it never earned.
        _LAST["at"], _LAST["clean"] = now, True
        return None

    _LAST["at"], _LAST["clean"] = now, False

    result: dict = {}
    if alerts:
        result["CHANGED"] = alerts
        result["impact"] = (
            "Scoring or roster shape moved, so replacement levels, VOR, tiers and "
            "every ranking derived from them are now stale. Re-run your last "
            "command to get corrected numbers."
        )
    if unreachable:
        result["unverified"] = (
            f"Could not reach Sleeper to verify: {', '.join(unreachable)}"
        )
    return result


def invalidate() -> None:
    """Drop the grace window so the next check does a real read.

    Called by refresh_data: if someone explicitly asks to re-read everything,
    serving them a settings verdict cached from half an hour ago is exactly
    the wrong answer.
    """
    _LAST["at"], _LAST["clean"] = 0.0, False


def status() -> dict:
    """Current fingerprint of every league, for explicit inspection."""
    try:
        cfg = context.load_config()
    except Exception:
        return {"error": "no config"}

    leagues = cfg.get("leagues") or []
    fetched = _fetch_all([e["league_id"] for e in leagues if e.get("league_id")])

    out = {}
    for entry in leagues:
        lid = entry.get("league_id")
        lg = fetched.get(lid) if lid else None
        if not lg:
            out[entry.get("name") or lid] = {"error": "unreachable"}
            continue
        fp = fingerprint(lg)
        out[entry.get("name") or lid] = {
            "teams": fp["total_rosters"],
            "roster_positions": fp["roster_positions"],
            "scoring_keys": len(fp["scoring_settings"]),
            "ppr": fp["scoring_settings"].get("rec"),
            "pass_td": fp["scoring_settings"].get("pass_td"),
            "settings": fp["settings"],
        }
    return out
