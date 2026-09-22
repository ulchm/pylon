"""Director output types: a Shot (what to point the camera at) and a Decision
(a logged cut to a new shot).

ShotKind and ShotFlavor are re-exported from show/contract.py, where they live because
the overlay and the actuator both read them and none of them should have to
import the director to do so.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..show.contract import ShotFlavor, ShotKind

__all__ = ["Decision", "Shot", "ShotFlavor", "ShotKind"]


@dataclass(frozen=True)
class Shot:
    kind: str
    key: str            # stable identity used for hysteresis ("same shot?" comparison)
    target_idx: int     # car index to point the camera at (attacker, in a battle)
    label: str
    pair: tuple[int, int] | None = None  # (ahead_idx, behind_idx) for battles
    flavor: str = ""    # ShotFlavor.*; angle-selection hint, not part of shot identity


@dataclass(frozen=True)
class Decision:
    time: float         # session time of the cut
    shot: Shot
    score: float
    reason: str         # why we cut: opening / stronger / variety / incident / shot ended
    prev_held: float    # seconds the previous shot was on screen (0.0 for the first cut)
