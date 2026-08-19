"""Handcuffs - the backup who inherits the job if your starter goes down.

Handcuffing matters far more at RB than anywhere else, and the reason is
structural rather than tactical: running back value is driven by volume, and
volume transfers almost intact to whoever is next on the depth chart. When a
workhorse back is lost, his backup frequently becomes an immediate RB2. No
other position works like that - a WR2 does not inherit the WR1's talent, and
a backup QB rarely inherits his starter's efficiency.

So the payoff is a function of two things: how much volume the starter is
absorbing, and how cleanly the backup would step into it. A committee backfield
has little to handcuff, because the volume is already split.
"""

from __future__ import annotations

from . import sleeper

# Positions where a backup meaningfully inherits the role.
HANDCUFF_POSITIONS = {"RB"}


def _depth_chart(team: str, position: str) -> list[dict]:
    """Active players at one team/position, ordered by depth chart."""
    players = sleeper.all_players()
    pool = [
        v
        for v in players.values()
        if v.get("team") == team
        and v.get("position") == position
        and v.get("active")
        and v.get("depth_chart_order") is not None
    ]
    return sorted(pool, key=lambda v: v["depth_chart_order"])


def find_handcuff(starter: dict, rows_by_id: dict[str, dict]) -> dict | None:
    """The next man up behind a given starter, with his standalone value."""
    team, pos = starter.get("team"), starter.get("position")
    if not team or pos not in HANDCUFF_POSITIONS:
        return None

    chart = _depth_chart(team, pos)
    if not chart:
        return None

    starter_order = next(
        (
            v["depth_chart_order"]
            for v in chart
            if v.get("player_id") == starter.get("player_id")
        ),
        None,
    )
    if starter_order is None:
        return None

    backup = next((v for v in chart if v["depth_chart_order"] > starter_order), None)
    if not backup:
        return None

    backup_row = rows_by_id.get(backup.get("player_id"))
    return {
        "player_id": backup.get("player_id"),
        "name": backup.get("full_name"),
        "team": team,
        "position": pos,
        "depth_chart_order": backup["depth_chart_order"],
        "standalone_points": (backup_row or {}).get("points"),
        "standalone_vor": (backup_row or {}).get("vor"),
        "adp": (backup_row or {}).get("adp"),
    }


def handcuff_priority(starter: dict, backup: dict | None) -> dict:
    """How much this particular handcuff is worth owning.

    The signal is the starter's workload concentration. A back carrying most of
    his team's volume leaves a large, transferable role behind him; a back in a
    committee does not.
    """
    if not backup:
        return {"priority": "none", "reason": "no clear backup on the depth chart"}

    vor = starter.get("vor") or 0
    usage = starter.get("prior_usage") or {}
    carry_share = usage.get("carry_share")
    snap_share = usage.get("snap_share")

    reasons: list[str] = []
    score = 0

    if vor >= 100:
        score += 2
        reasons.append(f"starter is a premium asset (VOR {vor:.0f})")
    elif vor >= 50:
        score += 1
        reasons.append(f"starter is a solid starter (VOR {vor:.0f})")

    if carry_share is not None:
        if carry_share >= 60:
            score += 2
            reasons.append(f"workhorse role - {carry_share:.0f}% of carries would transfer")
        elif carry_share >= 45:
            score += 1
            reasons.append(f"clear lead back ({carry_share:.0f}% of carries)")
        else:
            score -= 1
            reasons.append(
                f"committee backfield ({carry_share:.0f}% of carries) - less to inherit"
            )

    if snap_share is not None and snap_share >= 70:
        score += 1
        reasons.append(f"on the field for {snap_share:.0f}% of snaps")

    avail = starter.get("availability_pct")
    if avail is not None and avail < 85:
        score += 1
        reasons.append(f"starter has missed time before ({avail:.0f}% available)")

    priority = (
        "high" if score >= 4 else "medium" if score >= 2 else "low"
    )
    return {"priority": priority, "score": score, "reason": "; ".join(reasons) or "no strong signal"}


def roster_handcuffs(
    owned: list[dict],
    rows_by_id: dict[str, dict],
    taken: set[str] | None = None,
) -> list[dict]:
    """Handcuff report for every handcuff-worthy player on a roster."""
    taken = taken or set()
    out: list[dict] = []

    for p in owned:
        if p.get("position") not in HANDCUFF_POSITIONS:
            continue
        backup = find_handcuff(p, rows_by_id)
        prio = handcuff_priority(p, backup)

        out.append(
            {
                "starter": p["name"],
                "team": p.get("team"),
                "starter_vor": p.get("vor"),
                "handcuff": (backup or {}).get("name"),
                "handcuff_rostered": (backup or {}).get("player_id") in taken
                if backup
                else None,
                "handcuff_standalone_vor": (backup or {}).get("standalone_vor"),
                "handcuff_adp": (backup or {}).get("adp"),
                **prio,
            }
        )

    rank = {"high": 0, "medium": 1, "low": 2, "none": 3}
    out.sort(key=lambda x: rank.get(x["priority"], 9))
    return out
