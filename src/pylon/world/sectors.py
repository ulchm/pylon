"""Sector pace: which cars are on a quick lap, sector by sector, for the whole field.

iRacing publishes no sector times for anybody but the player, and on the broadcast
box the player is the spectator: every `Lap*` / `LapDelta*` channel is dead there,
and the per-car set (`CarIdx*`) carries lap DISTANCE, the last lap and the best lap,
nothing in between (checked against recordings/capture2, 2026-09-21). What it does
publish is where the sectors start (`SplitTimeInfo` in the session YAML, at the Glen
0.0 / 0.313 / 0.523 / 0.723), so the sector times are measured here: the session
clock at each boundary crossing, interpolated between the two frames either side of
it. At the bridge's 10Hz a frame is ~0.1s of running and a car's speed barely changes
across one, so a split lands within a few hundredths. Good enough for "on pace" and
"quickest of anyone through sector one"; never quoted as a lap time, which is the
sim's own `CarIdxLastLapTime` and exact.

What a car on a quick lap is compared against is CUMULATIVE, the way a timing
screen's delta works: the time to this boundary against the time the reference lap
took to the same boundary. Two references, the car's own best lap and the session's
best lap, each kept as the splits of the lap that actually set it. (Comparing against
the best-ever sector of each, the "ideal lap", reads every real lap as off pace.)
Both are references WE MEASURED, so a broadcast that joined mid-session holds a session
best that is not the session's: each is only offered while it agrees with the sim's
own best-lap channel, see `update`.

A lap counts only if it began at the line (not from the pit exit) and stayed out of
the lane. A lap that left the world or scrubbed the tape is dropped, not scored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..telemetry.frame import SessionInfo

# The most a car can move on between two frames and still be driving (a reset to the
# pits, a tow and a scrub are all bigger); a crossing found across a step this size is
# not a crossing. Same figure and reasoning as the rest of the session's records.
_STEP_MAX = 0.25
# Our measured lap against the sim's: the references are only offered while they agree
# to this. Larger than the interpolation error, smaller than any two real laps differ.
_AGREE_S = 0.15
# A sector is "quickest" only by a margin the interpolation cannot fake.
_SECTOR_EPS = 0.02
# Progress is a float and a boundary can land ON a frame: 2.3 - 0.3 is 1.9999999999999998
# in binary, and a crossing tested without slack is missed on that frame and then, being
# behind the car, on every frame after it. Applied to both ends of the step the same way,
# so a crossing counts exactly once whichever side of the boundary the frame falls.
_EPS = 1e-9


@dataclass(frozen=True)
class LapRef:
    """A completed lap as a reference: its time and its cumulative splits."""
    idx: int
    time: float
    splits: tuple[float, ...]     # time to boundaries 1..n-1


@dataclass(frozen=True)
class LapPace:
    """One car's current lap, as of the last sector boundary it crossed.

    `sector` is how many boundaries it has crossed since the line: 1 after the first
    sector, up to n-1 mid-lap, and n at the line, when `done` is set and the deltas are
    the result. Deltas are negative when the car is UP on the reference, None when
    there is no reference (nobody has completed a clean lap yet, or the one we hold
    disagrees with the sim's board). `purple` / `green` describe the sector just
    completed: the quickest of the session, or of this car's own.
    """
    started: float                # session time the lap began: identifies the lap
    at: float                     # session time of the crossing this reports
    sector: int
    elapsed: float
    vs_own: float | None
    vs_best: float | None
    purple: bool
    green: bool
    done: bool = False

    def on_session_pace(self, margin: float) -> bool:
        """Within `margin` of the session's best lap at this point, or up on it. The
        margin is real: a car a few hundredths down at the first boundary is still on
        for the lap, and the split itself is only good to a few hundredths."""
        return self.vs_best is not None and self.vs_best <= margin

    @property
    def up_on_own(self) -> bool:
        """Up on this car's own best lap at this point. Strict: level is not up."""
        return self.vs_own is not None and self.vs_own < 0.0


@dataclass
class _Lap:
    started: float
    splits: list[float]
    clean: bool
    # The references this lap was last measured against, at its last boundary. The
    # result at the line is scored against THESE: the sim's board turns over on the
    # crossing frame with this very lap on it, and read fresh there it would disagree
    # with our reference by exactly the lap's improvement and withhold it.
    own_ref: LapRef | None = None
    session_ref: LapRef | None = None


def boundaries_from_info(info: SessionInfo) -> list[float]:
    """The sector start fractions, sorted, or [] for a feed without SplitTimeInfo (the
    synthetic source). A single boundary is no sectors at all."""
    raw = (info.raw.get("SplitTimeInfo") or {}).get("Sectors") or []
    out = []
    for s in raw:
        pct = s.get("SectorStartPct") if isinstance(s, dict) else None
        if isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0.0 <= pct < 1.0:
            out.append(float(pct))
    out = sorted(set(out))
    if len(out) < 2 or out[0] != 0.0:
        return []
    return out


class SectorTracker:
    def __init__(self, boundaries: list[float]):
        self.bounds = list(boundaries)
        self.n = len(self.bounds)
        self._prev: dict[int, tuple[float, float]] = {}      # idx -> (t, progress)
        self._lap: dict[int, _Lap] = {}
        self._sector_from: dict[int, tuple[int, float]] = {}  # idx -> (boundary, t crossed)
        self._dirty: set[int] = set()                         # in the lane since that crossing
        self.own_best: dict[int, LapRef] = {}
        self.session_best: LapRef | None = None
        self.own_best_sectors: dict[int, list[float | None]] = {}
        self.session_best_sectors: list[float | None] = [None] * self.n
        self.pace: dict[int, LapPace] = {}

    @property
    def enabled(self) -> bool:
        return self.n >= 2

    def reset(self, *, keep_refs: bool) -> None:
        """After a jump of the clock. The laps in progress belong to the old timeline;
        the references are this session's and survive a scrub, not a new session."""
        self._prev.clear()
        self._lap.clear()
        self._sector_from.clear()
        self._dirty.clear()
        self.pace.clear()
        if not keep_refs:
            self.own_best.clear()
            self.session_best = None
            self.own_best_sectors.clear()
            self.session_best_sectors = [None] * self.n

    # ------------------------------------------------------------------ feed
    def update(self, t: float, cars: dict[int, tuple[float, bool]],
               sim_best: dict[int, float | None]) -> None:
        """One frame: `cars` is idx -> (progress, on pit road) for every car in the
        world, `sim_best` each car's CarIdxBestLapTime (None for no lap).

        The sim's board is what keeps the references honest. Our own-best for a car is
        only a reference while it matches the sim's best for that car; our session best
        only while it matches the quickest on the sim's board. A broadcast that connects
        mid-session measures its first clean lap and would otherwise call every car
        "on pace for the best lap of the session" against a lap that was never that.
        """
        if not self.enabled:
            return
        gone = [i for i in self._prev if i not in cars]
        for i in gone:
            self._forget(i)
        board_best = min((b for b in sim_best.values() if b is not None and b > 0.0),
                         default=None)
        for i, (prog, in_lane) in cars.items():
            prev = self._prev.get(i)
            self._prev[i] = (t, prog)
            if in_lane:
                # The lap is no longer a flying lap, and the sector in hand is no
                # longer a sector: both wait for the next crossing of the line.
                self._dirty.add(i)
                lap = self._lap.get(i)
                if lap is not None:
                    lap.clean = False
                self.pace.pop(i, None)
            if prev is None:
                continue
            t0, p0 = prev
            step = prog - p0
            if not (0.0 < step < _STEP_MAX):
                if step < 0.0 or step >= _STEP_MAX:
                    self._forget(i)        # a reset, a tow, a scrub: not a lap
                continue
            for pos, k in self._crossings(p0, prog):
                frac = min(1.0, max(0.0, (pos - p0) / step))
                tc = t0 + frac * (t - t0)
                self._cross(i, k, tc, in_lane, sim_best.get(i), board_best)

    # ------------------------------------------------------------------ inside
    def _crossings(self, p0: float, p1: float) -> list[tuple[float, int]]:
        """(progress, boundary) for each boundary crossed moving from p0 to p1 (under a
        lap of movement), in road order."""
        out = []
        for k, b in enumerate(self.bounds):
            pos = math.floor(p1 + _EPS - b) + b
            if p0 + _EPS < pos <= p1 + _EPS:
                out.append((pos, k))
        return sorted(out)

    def _forget(self, i: int) -> None:
        self._prev.pop(i, None)
        self._lap.pop(i, None)
        self._sector_from.pop(i, None)
        self._dirty.discard(i)
        self.pace.pop(i, None)

    def _cross(self, i: int, k: int, tc: float, in_lane: bool,
               own_sim: float | None, board_best: float | None) -> None:
        # The sector just completed, whoever's lap it is on: a sector is a sector
        # provided the car drove it on the road from the previous boundary.
        sector_t: float | None = None
        frm = self._sector_from.get(i)
        prev_k = (k - 1) % self.n
        if frm is not None and frm[0] == prev_k and i not in self._dirty and not in_lane:
            sector_t = tc - frm[1]
        self._sector_from[i] = (k, tc)
        self._dirty.discard(i)
        if in_lane:
            self._dirty.add(i)
        purple = green = False
        if sector_t is not None and sector_t > 0.0:
            s = prev_k
            best = self.session_best_sectors[s]
            purple = best is None or sector_t < best - _SECTOR_EPS
            if purple:
                self.session_best_sectors[s] = sector_t
            mine = self.own_best_sectors.setdefault(i, [None] * self.n)
            green = mine[s] is None or sector_t < mine[s] - _SECTOR_EPS
            if green:
                mine[s] = sector_t

        lap = self._lap.get(i)
        if k == 0:
            # The line: the lap in hand is complete, and a new one begins.
            if lap is not None and lap.clean and len(lap.splits) == self.n - 1:
                self._complete(i, lap, tc, purple, green)
            else:
                self.pace.pop(i, None)
            self._lap[i] = _Lap(started=tc, splits=[], clean=not in_lane)
            return
        if lap is None or not lap.clean or len(lap.splits) != k - 1:
            # Not a flying lap (started from the pits, or a boundary was missed).
            if lap is not None:
                lap.clean = False
            self.pace.pop(i, None)
            return
        elapsed = tc - lap.started
        lap.splits.append(elapsed)
        lap.own_ref = self._own_ref(i, own_sim)
        lap.session_ref = self._session_ref(board_best)
        self.pace[i] = LapPace(
            started=lap.started, at=tc, sector=k, elapsed=elapsed,
            vs_own=self._delta(lap.own_ref, k, elapsed),
            vs_best=self._delta(lap.session_ref, k, elapsed),
            purple=purple, green=green,
        )

    def _complete(self, i: int, lap: _Lap, tc: float, purple: bool, green: bool) -> None:
        time = tc - lap.started
        if time <= 0.0:
            self.pace.pop(i, None)
            return
        # The result, against the references the lap was driven to (see _Lap): that
        # is what "did it come off" means.
        result = LapPace(
            started=lap.started, at=tc, sector=self.n, elapsed=time,
            vs_own=self._delta(lap.own_ref, self.n, time),
            vs_best=self._delta(lap.session_ref, self.n, time),
            purple=purple, green=green, done=True,
        )
        self.pace[i] = result
        ref = LapRef(i, time, tuple(lap.splits))
        mine = self.own_best.get(i)
        if mine is None or time < mine.time:
            self.own_best[i] = ref
        if self.session_best is None or time < self.session_best.time:
            self.session_best = ref

    def _own_ref(self, i: int, own_sim: float | None) -> LapRef | None:
        ref = self.own_best.get(i)
        if ref is None:
            return None
        # A best on the sim's board that we never measured: ours is not this car's best.
        if own_sim is not None and own_sim > 0.0 and own_sim < ref.time - _AGREE_S:
            return None
        return ref

    def _session_ref(self, board_best: float | None) -> LapRef | None:
        ref = self.session_best
        if ref is None:
            return None
        if board_best is not None and board_best < ref.time - _AGREE_S:
            return None
        return ref

    @staticmethod
    def _delta(ref: LapRef | None, k: int, elapsed: float) -> float | None:
        """Time to boundary k on this lap, against the reference lap's time to it."""
        if ref is None:
            return None
        if k == len(ref.splits) + 1:
            return elapsed - ref.time
        if 1 <= k <= len(ref.splits):
            return elapsed - ref.splits[k - 1]
        return None
