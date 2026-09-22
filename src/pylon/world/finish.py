"""Who has taken the flag: the finish as a sequence of line crossings, not an instant.

Why this exists: Round 1 (Watkins Glen, 2026-09-20) ended with the camera cutting
off a 0.04s fight for P4 the moment the chequer flew, sitting on the winner for 24s
while that pair crossed the line unseen, then walking down the order one car at a
time as each finisher reset to the pits. Nothing in the show knew that a race
finishes one car at a time: the director stopped scoring battles because the green
flag was gone, the resets were read as pit stops, and the closing card came up
while cars were still racing to the line.

The two facts here are different moments and are kept apart:

  * The FLAG is out (`flag_at`): `SessionSnapshot.is_checkered` went true. On a
    lap-count race that is the leader crossing the line; on a timed race the sim can
    enter CHECKERED at the time limit with the leader most of a lap from the line
    (seen on the Spa recording), so the flag
    being out says nothing about who has crossed.
  * A car has FINISHED (`finished_at[idx]`): its lap count went up after the flag,
    i.e. it crossed the line with the chequer showing. Lap counts are read from a
    reading taken `LOOK_BACK` before the flag frame, so a leader whose crossing IS
    the flag frame, or a photo-finish second a few frames behind, counts as crossed
    rather than as still racing. A lapped car finishes on fewer laps by the same
    rule: its next crossing is its flag.

`update` runs every frame from the world snapshot, and everything is reset when the
session clock jumps (a scrub, a rollover): a finish belongs to the timeline it was
watched on, which is the same rule the closing card works to.
"""

from __future__ import annotations

from collections import deque

from .model import WorldSnapshot, is_race_kind

# How far behind the flag frame the "laps before the flag" reading is taken. Long
# enough that a crossing in the same or the previous few frames still counts as
# crossed at any bridge rate we run (10-60 Hz), short enough that a car a full
# corner behind cannot have crossed inside it.
LOOK_BACK = 1.0


class FinishTracker:
    def __init__(self) -> None:
        self.flag_at: float | None = None
        self.finished_at: dict[int, float] = {}
        self._laps_at_flag: dict[int, int] = {}
        self._history: deque[tuple[float, dict[int, int]]] = deque()

    def reset(self) -> None:
        self.flag_at = None
        self.finished_at.clear()
        self._laps_at_flag.clear()
        self._history.clear()

    # ------------------------------------------------------------------ queries
    @property
    def flag_out(self) -> bool:
        return self.flag_at is not None

    def finished(self, idx: int) -> bool:
        return idx in self.finished_at

    def crossed_within(self, idx: int, t: float, window: float) -> bool:
        """Took the flag less than `window` seconds ago."""
        at = self.finished_at.get(idx)
        return at is not None and t - at < window

    # ------------------------------------------------------------------ feed
    def update(self, snap: WorldSnapshot, t: float) -> list[int]:
        """Feed one frame. Returns the cars that took the flag THIS frame, in
        running order, so a caller can react to each crossing once."""
        laps = {c.idx: c.lap_completed for c in snap.cars.values()}
        s = snap.session
        if not (is_race_kind(s.session_kind) and s.is_checkered):
            # Not a race finish. Keep the recent readings rolling so the frame the
            # flag appears on has something to look back to; forget an old finish
            # once the flag has gone (a new session, a restart).
            if self.flag_out:
                self.reset()
            self._remember(t, laps)
            return []
        if self.flag_at is None:
            self.flag_at = t
            self._laps_at_flag = dict(self._reading_before(t - LOOK_BACK) or laps)
        crossed = [idx for idx in snap.order
                   if idx not in self.finished_at
                   and laps.get(idx, 0) > self._laps_at_flag.get(idx, laps.get(idx, 0))]
        for idx in crossed:
            self.finished_at[idx] = t
        self._remember(t, laps)
        return crossed

    def _remember(self, t: float, laps: dict[int, int]) -> None:
        self._history.append((t, laps))
        # keep a little more than LOOK_BACK so there is always a reading older than it
        while len(self._history) > 1 and t - self._history[1][0] > LOOK_BACK * 2:
            self._history.popleft()

    def _reading_before(self, when: float) -> dict[int, int] | None:
        """The newest lap reading taken at or before `when`, or the oldest we have."""
        best = None
        for at, laps in self._history:
            if at <= when:
                best = laps
            else:
                break
        if best is None and self._history:
            best = self._history[0][1]
        return best
