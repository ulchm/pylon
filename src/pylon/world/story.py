"""Human-readable "current story" from a WorldSnapshot.

A Phase 1 deliverable and a debugging aid: it surfaces the leader, the closest
battles, and this tick's events. The interest heuristic here is a preview of the
director's scoring in Phase 2, deliberately kept simple.
"""

from __future__ import annotations

from .model import EventKind, WorldSnapshot

BATTLE_GAP = 1.0  # seconds; pairs closer than this are "battling"


def battles(snap: WorldSnapshot, max_gap: float = BATTLE_GAP):
    """Adjacent pairs within max_gap, ranked by a rough interest score.

    Uses the continuous on-track gap (track_gap_ahead), not CarIdxF2Time: the latter
    is a per-lap staircase that reads 0 until a car completes a lap, so it flags the
    whole field as a 0.00s battle at the start and hides every mid-lap catch.
    """
    out = []
    for i in snap.order:
        c = snap.cars[i]
        gap = c.track_gap_ahead
        if gap is None or c.car_ahead_idx is None:
            continue
        if not (0.0 <= gap <= max_gap):
            continue
        a = snap.cars[c.car_ahead_idx]
        interest = (
            (max_gap - gap)
            + max(0.0, c.closing_rate or 0.0) * 2.0
            + 1.0 / max(a.position, 1)
        )
        out.append((interest, a, c))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def render_story(snap: WorldSnapshot, top_battles: int = 3) -> list[str]:
    s = snap.session
    tag = ("RED" if s.is_red else "YELLOW" if s.is_yellow
           else "CHK" if s.is_checkered else "GREEN" if s.is_green else "-")
    leader = snap.leader()
    ld = f"P1 #{leader.number} {leader.name or ''}".rstrip() if leader else "-"
    lines = [f"[t={snap.session_time:6.1f}s] {tag}  leader: {ld}"]

    for _interest, a, c in battles(snap)[:top_battles]:
        cr = c.closing_rate or 0.0
        arrow = f"  closing {cr:+.2f}s/s" if abs(cr) > 0.01 else ""
        lines.append(
            f"    BATTLE  P{a.position} #{a.number} vs P{c.position} #{c.number}  "
            f"{c.track_gap_ahead:.2f}s{arrow}"
        )

    for e in snap.events:
        if e.kind == EventKind.OVERTAKE:
            p = snap.cars.get(e.car_idx)
            o = snap.cars.get(e.other_idx)
            if p and o:
                lines.append(f"    OVERTAKE  #{p.number} passes #{o.number} for P{e.position}")
        elif e.kind == EventKind.INCIDENT:
            c = snap.cars.get(e.car_idx)
            if c:
                o = snap.cars.get(e.other_idx) if e.other_idx is not None else None
                with_who = f" with #{o.number}" if o else ""
                # practice sessions report no running order, so there is no P to show
                at = f" at P{e.position}" if e.position else ""
                lines.append(f"    INCIDENT  [{e.severity or '?'}] #{c.number}{with_who} "
                             f"({e.detail}){at}")
        elif e.kind == EventKind.PIT_ENTRY:
            c = snap.cars.get(e.car_idx)
            if c:
                lines.append(f"    PIT       #{c.number} enters pit road")
        elif e.kind == EventKind.PIT_RESET:
            c = snap.cars.get(e.car_idx)
            if c:
                lines.append(f"    PIT       #{c.number} reset to the pits (not a stop)")
        elif e.kind == EventKind.PIT_EXIT:
            c = snap.cars.get(e.car_idx)
            if c:
                lines.append(f"    PIT       #{c.number} exits pit road")

    return lines
