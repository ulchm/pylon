"""Does the picture match the shot?

The camera pipeline is open loop everywhere else in this package: the director
decides a Shot, the actuator turns it into a CamCommand, the controller sends it,
and nothing ever looks to see where the camera ended up. Every consumer
downstream renders the director's INTENT and assumes it became reality.

On 2026-08-02 at Oulton Park it had not. `CamCarIdx` read 5 (#154 Puga) while the
director's shot was car 2 (#9 Moreno) and had been for a long stretch, and the
tower and the pop-ins both described the car that was not on screen. There was no
signal anywhere (no log line, no badge), and the only cure available to the
operator was restarting the director, which they had no way to know was needed.
That is issue #68, and this is the loop being closed.

## What it does, and what it deliberately does not

**It re-asserts; it does not adopt.** On sustained disagreement the answer is to
send the command again, not to rewrite the director's shot to match the picture.
Something moved the camera and the director is the director: adopting would hand
the running of the broadcast to whatever did it, and the failure mode of
re-asserting is a visible fight rather than a silent handover.

**Only the target CAR is reconciled, never the angle.** `CamGroupNumber` is
carried on the snapshot but is not compared: the angle rotator changes group
every few seconds on purpose, so a group that differs from the last command sent
is the system working correctly.

**A pair is a subject, not a car.** A battle shot names one car as the target and
carries both in `pair`; iRacing framing either of them is the shot working, so
both count as agreement.

**Not knowing is not disagreement.** A feed with no camera channels (the
synthetic source, and the two-box live path until the sim-box agent is rebuilt)
reconciles nothing at all and sends no commands. Absent must never read as "the
camera is on car zero", which is a real and very ordinary car.

**The debounce is not optional.** The camera legitimately lags a command by a
frame or two, and a replay excursion moves it somewhere else on purpose. So
disagreement has to persist before it means anything, and the caller must
`reset()` when it takes the camera away deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..director.model import Shot
from ..world import CameraView, WorldSnapshot

#: How long the picture and the shot must disagree before it means anything, in
#: session seconds. Comfortably longer than the frame or two the camera takes to
#: obey a command at 10-15Hz, and shorter than the 9s angle refresh, so a real
#: divergence is caught rather than waiting for the next re-frame to maybe fix it.
DEBOUNCE = 2.5


@dataclass(frozen=True)
class CameraDrift:
    """The picture and the shot have disagreed for long enough to act on."""

    #: Car index the shot asked for, and the one iRacing is actually showing.
    wanted: int
    actual: int
    #: How long they have disagreed, in session seconds.
    held: float
    #: Session time this was noticed, so a log line reads on the same clock as a cut.
    at: float
    #: The shot that is not on screen, for a log line that means something.
    shot: Shot

    def __str__(self) -> str:
        return (f"camera drifted: shot wants car {self.wanted} "
                f"({self.shot.kind} {self.shot.label!r}) but the sim is showing car "
                f"{self.actual}, {self.held:.1f}s")


def subject_cars(shot: Shot | None) -> frozenset[int]:
    """Every car index that counts as this shot being on screen."""
    if shot is None:
        return frozenset()
    return frozenset({shot.target_idx, *(shot.pair or ())})


class CameraWatch:
    """Watches one connection's camera against the director's shot."""

    def __init__(self, debounce: float = DEBOUNCE):
        self.debounce = debounce
        #: Session time the current disagreement began, or None when they agree.
        self._since: float | None = None
        #: The last reading, purely so a caller can report the state it is in.
        self._last: CameraView | None = None

    def reset(self) -> None:
        """Forget any disagreement in progress.

        Called when the camera is taken away on purpose (a replay excursion) and
        when it comes back. Without it, the frame we return on would compare a
        live shot against wherever the tape left the camera and re-assert
        immediately, and the excursion's own isolation would be broken from the
        other side.
        """
        self._since = None

    @property
    def drifting(self) -> bool:
        """Whether a disagreement is currently being timed (below the debounce)."""
        return self._since is not None

    @property
    def view(self) -> CameraView | None:
        """The last camera reading, or None if the feed has never carried one."""
        return self._last

    def observe(self, snap: WorldSnapshot, shot: Shot | None,
                *, t: float | None = None) -> CameraDrift | None:
        """One frame. A CameraDrift when the command is worth re-sending, else None.

        Returning a drift restarts the clock, so a camera that stays away is
        re-asserted once per debounce rather than on every frame.
        """
        t = snap.session_time if t is None else t
        self._last = snap.camera if snap.camera is not None else self._last

        # A discontinuous clock makes the elapsed time meaningless: a replay seek
        # can leave `_since` in the future, and the subtraction below would then
        # never reach the debounce again. Same reseat the director does.
        if snap.time_jump is not None:
            self._since = None
            return None

        wanted = subject_cars(shot)
        actual = snap.camera.car_idx if snap.camera is not None else None
        if not wanted or actual is None or actual in wanted:
            # Agreement, no shot, or no idea. All three are "nothing to do", and
            # keeping them one branch is the point: only a POSITIVE reading that
            # positively disagrees is ever acted on.
            self._since = None
            return None

        if self._since is None:
            self._since = t
            return None
        held = t - self._since
        if held < self.debounce:
            return None
        self._since = t
        assert shot is not None                      # implied by a non-empty `wanted`
        return CameraDrift(wanted=shot.target_idx, actual=actual, held=held,
                           at=t, shot=shot)
