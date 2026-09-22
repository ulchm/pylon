"""Off-track excursions, classified into IncidentSeverity tiers: the episode machine.

Going off is NOT an incident by itself: a quick lap uses the kerbs and the exit road,
and a fast human exploits track limits constantly. The original "surface -> OffTrack is
a clean incident signal" reading came from an AI race, where nobody does that
(DESIGN.md section 14). Treating every surface transition as a full incident is what
made the camera abandon good racing, so an off only escalates if it actually goes wrong.

OFF_TRACK still fires on the bare surface transition, as it always has, but INCIDENT
fires ONCE PER EPISODE, at the tier the episode turns out to deserve, which is not
knowable at the transition:

  MAJOR     immediately, when a second car goes off in the same place at the same
            time. A wreck must be able to take the camera at once.
  MODERATE  after INCIDENT_CONFIRM, if the car is still off the road and well down on
            the pace it carried in: a spin, a trip through the gravel.
  MINOR     on rejoining, for an excursion that escalated to neither. "This was only
            track limits" is a verdict you can only reach once it is over, and
            waiting means one moment never produces two lines.

The tiers are what let the director keep the camera on the racing through a
track-limits moment while still cutting instantly to a real crash; everything downstream reads the
same field to decide how excited to sound.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..telemetry.constants import TrackSurface
from .channels import Channels
from .model import Event, EventKind, IncidentSeverity

INCIDENT_CONFIRM = 1.2     # s still off the road before a solo excursion is more than
                           # track limits (cf. the director's trouble_confirm = 0.8)
INCIDENT_SLOW_FRAC = 0.65  # ...and only if the car is down to this fraction of the pace
                           # it carried off the road: a fast lap through the runoff costs
                           # nothing, a spin costs everything
INCIDENT_PAIR_WINDOW = 1.5  # s: two cars leaving the road within this of each other...
INCIDENT_PAIR_M = 75.0      # ...and this close on track = one shared moment, i.e. contact.
                            # Metres, not laps, so it means the same at Spa and at Lime Rock.


class IncidentTracker:
    """Per-car off-track episodes, and the events they earn.

    Everything here answers "what changed since last frame", so a clock jump resets it
    (see WorldModel._reset_motion): after a scrub, a car that has been off the road for
    a minute must not look like it just went off.
    """

    def __init__(self, track_len: float) -> None:
        self.track_len = track_len
        self._prev_surface: dict[int, int] = {}
        # per off-track episode, for severity: when it started, the pace the car
        # carried off the road, and the worst tier already reported for it.
        self._off_since: dict[int, float] = {}
        self._off_speed: dict[int, float] = {}
        self._off_severity: dict[int, str] = {}
        # idx -> (when, progress) of the last off, kept for INCIDENT_PAIR_WINDOW so a
        # second car going off nearby can be recognised as the same moment.
        self._recent_off: dict[int, tuple[float, float]] = {}

    def reset(self) -> None:
        self._prev_surface.clear()
        self._off_since.clear()
        self._off_speed.clear()
        self._off_severity.clear()
        self._recent_off.clear()

    def events(self, active: list[int], ch: Channels, t: float,
               progress: Mapping[int, float], speed: Mapping[int, float]) -> list[Event]:
        """This frame's OFF_TRACK and INCIDENT events. Call once per frame, in order."""
        events: list[Event] = []

        def at(i: int) -> int | None:
            p = ch.position(i)
            return p if p > 0 else None

        # 1) transitions: start (or close out) an off-track episode
        fresh: list[int] = []
        for i in active:
            ps = self._prev_surface.get(i)
            off = ch.surface(i) == TrackSurface.OFF_TRACK
            if ps is not None and ps != TrackSurface.OFF_TRACK and off:
                events.append(Event(EventKind.OFF_TRACK, i, position=at(i)))
                self._off_since[i] = t
                self._off_speed[i] = speed.get(i, 0.0)
                self._off_severity[i] = ""
                self._recent_off[i] = (t, progress[i])
                fresh.append(i)
            elif not off and self._off_since.pop(i, None) is not None:
                # back on the road (or in the pits): if it never escalated, THAT is
                # what makes it minor, and only now can we say so.
                if not self._off_severity.pop(i, ""):
                    events.append(Event(EventKind.INCIDENT, i, position=at(i),
                                        detail="off-track", severity=IncidentSeverity.MINOR))
                self._off_speed.pop(i, None)
        # a car that left the world mid-episode (towed, garaged) has no episode to
        # close: drop it, so returning to the track months later isn't "an off"
        for i in list(self._off_since):
            if i not in progress:
                self._off_since.pop(i, None)
                self._off_speed.pop(i, None)
                self._off_severity.pop(i, None)
        self._recent_off = {
            i: v for i, v in self._recent_off.items() if t - v[0] <= INCIDENT_PAIR_WINDOW
        }

        # 2) MAJOR: two cars in the same moment and the same place. This is the one
        # collision signal a per-car detector structurally cannot see, and the only
        # thing besides a stricken car that earns an interrupt (see director/config).
        for i in fresh:
            if self._off_severity.get(i) == IncidentSeverity.MAJOR:
                continue  # already paired this tick, by the partner's turn in the loop
            j = self._collision_partner(i, t, progress)
            if j is None:
                continue
            self._off_severity[i] = IncidentSeverity.MAJOR
            if j in self._off_since:
                self._off_severity[j] = IncidentSeverity.MAJOR  # it is in this one too
            # frame the better-placed car; the other rides along as other_idx
            lead, other = (i, j) if (at(i) or 999) <= (at(j) or 999) else (j, i)
            # The moment is when the FIRST of them left the road, not this frame: the
            # partner's off is what let us call it, up to INCIDENT_PAIR_WINDOW later.
            # The hit itself precedes even that; a replay's lead-in covers the rest.
            moment = min(t, self._recent_off[j][0])
            events.append(Event(EventKind.INCIDENT, lead, other_idx=other, position=at(lead),
                                detail="contact", severity=IncidentSeverity.MAJOR,
                                at=moment))

        # 3) MODERATE: still off the road a beat later, and it has cost real time.
        # Both halves matter: a fast lap through the runoff is sustained but costs
        # nothing, and a one-frame surface flicker costs nothing either.
        for i in active:
            t0 = self._off_since.get(i)
            if t0 is None or self._off_severity.get(i, "") or t - t0 < INCIDENT_CONFIRM:
                continue
            entry = self._off_speed.get(i, 0.0)
            if entry > 0.0 and speed.get(i, 0.0) <= INCIDENT_SLOW_FRAC * entry:
                self._off_severity[i] = IncidentSeverity.MODERATE
                events.append(Event(EventKind.INCIDENT, i, position=at(i),
                                    detail="off, losing time",
                                    severity=IncidentSeverity.MODERATE, at=t0))

        self._prev_surface = {i: ch.surface(i) for i in active}
        return events

    def _collision_partner(self, i: int, t: float, progress: Mapping[int, float]) -> int | None:
        """The nearest car that also left the road within INCIDENT_PAIR_WINDOW and is
        within INCIDENT_PAIR_M of `i` on track. Distance is folded to the lap so a
        lapped car tangling with a leader at the same corner still counts."""
        best: tuple[int, float] | None = None
        for j, (tj, pj) in self._recent_off.items():
            if j == i or j not in progress or t - tj > INCIDENT_PAIR_WINDOW:
                continue
            frac = abs(progress[i] - pj) % 1.0
            metres = min(frac, 1.0 - frac) * self.track_len
            if metres <= INCIDENT_PAIR_M and (best is None or metres < best[1]):
                best = (j, metres)
        return best[0] if best else None
