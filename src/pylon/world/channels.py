"""One frame's per-car channels, read once and indexed safely.

iRacing's CarIdx* channels are 64-slot arrays, one entry per possible car, and a feed is
not obliged to carry every one of them: the synthetic source has no lap times, an older
bridge streamed a fixed list, a capture enumerates everything. The builder used to read
each list with `frame.get(name) or []` and then index some of them bare (`pos[i]`,
`lapc[i]`, `onpit[i]`) while guarding others (`lap[i] if i < len(lap) else -1`), so a
channel the feed lacked was fine in one method and an IndexError on frame one in the
next. Every read goes through here now, with one default per channel that already means
"no data" to its consumers, and the sentinel-stripping that sat in _car_state's helpers
lives beside the channel it belongs to.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..telemetry.constants import DRIVER_FLAG_MASK, TrackSurface
from ..telemetry.frame import Frame


def _at(seq: Sequence, i: int, default):
    return seq[i] if i < len(seq) else default


def _number(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def lap_time(channel: Sequence, i: int) -> float | None:
    """One car's lap time from a CarIdx lap-time channel, with the sentinel gone.

    Strictly > 0: -1.0 is iRacing's "no lap yet" sentinel, and a 0.0 lap is not a lap
    either (CarIdxF2Time's zero-until-a-lap habit is the same trap wearing a different
    number: DESIGN.md 14). The sentinel dies HERE rather than in each consumer, so a
    None downstream means "no lap", never "a lap we failed to parse".
    """
    v = _number(_at(channel, i, None))
    return v if v is not None and v > 0 else None


class Channels:
    """The per-car channels of one frame.

    `n` is the slot count, taken from CarIdxTrackSurface: the one channel every feed
    carries, because without it nothing is in the world at all.
    """

    __slots__ = (
        "blt",
        "cflags",
        "cls",
        "cpos",
        "est",
        "f2",
        "lapc",
        "laps",
        "ldp",
        "llt",
        "onpit",
        "pos",
        "surf",
    )

    def __init__(self, frame: Frame):
        get = frame.get
        self.pos: Sequence = get("CarIdxPosition") or []
        self.cpos: Sequence = get("CarIdxClassPosition") or []
        self.ldp: Sequence = get("CarIdxLapDistPct") or []
        self.laps: Sequence = get("CarIdxLap") or []
        self.lapc: Sequence = get("CarIdxLapCompleted") or []
        self.surf: Sequence = get("CarIdxTrackSurface") or []
        self.onpit: Sequence = get("CarIdxOnPitRoad") or []
        self.cls: Sequence = get("CarIdxClass") or []
        self.f2: Sequence = get("CarIdxF2Time") or []           # SDK time behind leader
        self.est: Sequence = get("CarIdxEstTime") or []         # seconds from the line (#55)
        self.llt: Sequence = get("CarIdxLastLapTime") or []     # -1.0 until a lap is done
        self.blt: Sequence = get("CarIdxBestLapTime") or []     # ...and the same for the best
        self.cflags: Sequence = get("CarIdxSessionFlags") or []  # per-car; mostly background

    @property
    def n(self) -> int:
        return len(self.surf)

    def position(self, i: int) -> int:
        """Sheet position. 0 is "unscored", which is also what an absent channel reads."""
        return _at(self.pos, i, 0) or 0

    def class_position(self, i: int) -> int:
        return _at(self.cpos, i, 0) or 0

    def lap(self, i: int) -> int:
        return _at(self.laps, i, -1)

    def lap_completed(self, i: int) -> int:
        """-1 until the car first crosses the line, and -1 for an absent channel."""
        return _at(self.lapc, i, -1)

    def lap_dist_pct(self, i: int) -> float:
        """Fraction of the lap; the sim's own -1.0 for "unknown", and for absent."""
        return _at(self.ldp, i, -1.0)

    def surface(self, i: int) -> int:
        return _at(self.surf, i, TrackSurface.NOT_IN_WORLD)

    def on_pit_road(self, i: int) -> bool:
        return bool(_at(self.onpit, i, False))

    def car_class(self, i: int) -> int | None:
        return _at(self.cls, i, None)

    def f2_time(self, i: int) -> float | None:
        """Time behind the leader, or None for the sim's negative "no time yet"."""
        v = _number(_at(self.f2, i, None))
        return v if v is not None and v >= 0 else None

    def est_time(self, i: int) -> float | None:
        """Seconds from the line to the car. Non-positive is "no data" for this slot,
        not a car sitting on the line."""
        v = _number(_at(self.est, i, None))
        return v if v is not None and v > 0.0 else None

    def last_lap(self, i: int) -> float | None:
        return lap_time(self.llt, i)

    def best_lap(self, i: int) -> float | None:
        return lap_time(self.blt, i)

    def driver_flags(self, i: int) -> int:
        """One car's driver-directed flags, with the background bits masked off.

        `servicible` is the whole reason this is not a straight read: it is not a flag
        (the SDK says so) and it is set on nearly every car for nearly every frame, so
        the raw value is truthy almost always. Masking here means a nonzero downstream
        really does mean "this car has been shown a flag": the same contract every
        other channel in this builder gets.
        """
        v = _at(self.cflags, i, None)
        return int(v) & DRIVER_FLAG_MASK if isinstance(v, (int, float)) else 0
