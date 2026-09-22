"""Camera-angle policy: pick *which* camera group frames a shot, and vary it.

iRacing exposes far more than the trackside TV cameras: in-car (Cockpit, Nose,
Gearbox, Roll Bar), chase (Chase, Far Chase, Rear Chase), world cams (Chopper,
Blimp, Scenic) and the pit-lane pair, on top of TV1/TV2/TV3 and the two big mixed
sets (TV Mixed, 68 cameras at Watkins Glen; TV Static, 21). The actuator knows the
target car and which groups this track has; this module decides which of those
angles to use for a shot and rotates through good ones so the broadcast never sits
on one flat look.

The COCKPIT group is out of the classic and cinematic personalities (2026-09-21).
It is the driver's-eye view and it shows the mirrors, which the operator does not
want on air; at Round 1 it was one FOLLOW cut in four, 143 of ~630 cuts. The in-car
accents those personalities keep are the ones with no mirror in frame: Nose and
Gearbox. `onboard_forward` is the one personality that still features it, by name.
Measured at the same time: of the 22 groups the track offered, eleven were never cut
to at all (the two mixed TV sets among them, which is most of the "real broadcast"
look), so the follow and leader rotations now draw on those.

Choosing an angle is the single piece of directorial *taste* that lives in the
render domain, so it is pure, testable data + logic here (no SDK, no live
telemetry). A profile maps (shot kind, flavor) -> a ranked list of logical camera
names. The rotator resolves those names against the track's CameraMap, drops
groups the track lacks, rotates for variety, and avoids repeating the exact same
group on back-to-back cuts. If a track provides none of a shot's preferred angles,
it falls back to the map's default TV group so the cut still lands somewhere sane.

Three shipped personalities (DirectorConfig/ActuatorConfig pick one):
- classic:         trackside TV backbone, in-car/chase as accents (the default).
- onboard_forward: feature in-car heavily, especially the free leader and attackers.
- cinematic:       widest range, weaving chopper/blimp for drama.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..show.contract import ShotFlavor, ShotKind

if TYPE_CHECKING:  # avoid a runtime import cycle (actuator imports this module)
    from .actuator import CameraMap


class Angle:
    """Logical camera-group names. Must match iRacing CameraInfo GroupName strings
    (verified against the Spa capture); the CameraMap resolves them per track."""

    TV1 = "TV1"
    TV2 = "TV2"
    TV3 = "TV3"
    TV_MIXED = "TV Mixed"     # every trackside camera, the sim choosing among them
    TV_STATIC = "TV Static"   # fixed framings the cars drive through
    COCKPIT = "Cockpit"       # driver's eye, mirrors in frame (see the module note)
    GYRO = "Gyro"             # ...stabilised; the same view, the same mirrors
    NOSE = "Nose"
    GEARBOX = "Gearbox"
    ROLL_BAR = "Roll Bar"
    CHASE = "Chase"
    FAR_CHASE = "Far Chase"
    REAR_CHASE = "Rear Chase"
    CHOPPER = "Chopper"
    BLIMP = "Blimp"
    SCENIC = "Scenic"
    PIT_LANE = "Pit Lane"     # the lane-side cameras, tracking the car through its stop
    PIT_LANE_2 = "Pit Lane 2"


# The stop and the way out, the same in every personality. The lane cameras are the
# only ones that frame a car in its box; both groups are offered so two stops in a row
# do not come from the same wall. The rejoin wants the road the car is merging into and
# whoever is on it, which the lane cameras cannot see: chase first, then the trackside
# sets.
_PIT_PROFILES: dict[tuple[str, str], tuple[str, ...]] = {
    (ShotKind.PIT, ""): (Angle.PIT_LANE, Angle.PIT_LANE_2, Angle.CHASE, Angle.TV1),
    (ShotKind.FOLLOW, ShotFlavor.REJOIN):
        (Angle.CHASE, Angle.TV1, Angle.FAR_CHASE, Angle.TV_MIXED),
}


@dataclass(frozen=True)
class AnglePolicy:
    """A ranked list of camera angles per (shot kind, flavor), plus a default."""

    profiles: dict[tuple[str, str], tuple[str, ...]]
    default: tuple[str, ...] = (Angle.TV1, Angle.TV2, Angle.TV3)

    def ranked(self, kind: str, flavor: str) -> tuple[str, ...]:
        """Preferred angles for a shot: exact (kind, flavor), else the kind's
        default flavor, else the policy default."""
        return (
            self.profiles.get((kind, flavor))
            or self.profiles.get((kind, ""))
            or self.default
        )

    # --- shipped personalities --------------------------------------------
    @classmethod
    def classic(cls) -> AnglePolicy:
        """Trackside TV backbone, drawing on every trackside set the track has; nose,
        gearbox and chase as the accents. No cockpit (see the module note)."""
        return cls({
            (ShotKind.LEADER, ""):
                (Angle.TV1, Angle.TV_MIXED, Angle.TV3, Angle.CHASE, Angle.TV_STATIC, Angle.NOSE),
            (ShotKind.LEADER, ShotFlavor.SOLO):
                (Angle.TV1, Angle.NOSE, Angle.TV_MIXED, Angle.FAR_CHASE, Angle.TV3, Angle.BLIMP),
            (ShotKind.FOLLOW, ""):
                (Angle.TV1, Angle.TV_MIXED, Angle.TV3, Angle.CHASE, Angle.TV_STATIC,
                 Angle.NOSE, Angle.TV2, Angle.GEARBOX),
            (ShotKind.BATTLE, ""): (Angle.TV2, Angle.TV1, Angle.TV_MIXED, Angle.TV3, Angle.CHASE),
            (ShotKind.BATTLE, ShotFlavor.SIDE_BY_SIDE):
                (Angle.CHASE, Angle.TV2, Angle.TV1, Angle.NOSE, Angle.TV_STATIC),
            (ShotKind.BATTLE, ShotFlavor.PASS):
                (Angle.CHASE, Angle.NOSE, Angle.TV2, Angle.FAR_CHASE),
            (ShotKind.INCIDENT, ""): (Angle.TV1, Angle.TV3, Angle.CHOPPER),
            (ShotKind.INCIDENT, ShotFlavor.REPLAY): (Angle.TV1, Angle.TV2, Angle.TV3),
            (ShotKind.TROUBLE, ""): (Angle.TV1, Angle.CHOPPER, Angle.TV3, Angle.TV_MIXED),
            (ShotKind.FINISH, ""): (Angle.TV1, Angle.TV2, Angle.TV3),
            **_PIT_PROFILES,
        })

    @classmethod
    def onboard_forward(cls) -> AnglePolicy:
        """Feature in-car: cockpit/nose lead for the free leader and attackers. The one
        personality that still uses the cockpit view, mirrors and all."""
        return cls({
            (ShotKind.LEADER, ""): (Angle.TV1, Angle.TV2, Angle.TV3, Angle.CHASE),
            (ShotKind.LEADER, ShotFlavor.SOLO):
                (Angle.COCKPIT, Angle.NOSE, Angle.CHASE, Angle.TV1),
            (ShotKind.FOLLOW, ""): (Angle.COCKPIT, Angle.NOSE, Angle.CHASE, Angle.TV1),
            (ShotKind.BATTLE, ""): (Angle.TV2, Angle.CHASE, Angle.TV1, Angle.TV3),
            (ShotKind.BATTLE, ShotFlavor.SIDE_BY_SIDE):
                (Angle.NOSE, Angle.CHASE, Angle.TV2, Angle.FAR_CHASE),
            (ShotKind.BATTLE, ShotFlavor.PASS):
                (Angle.NOSE, Angle.COCKPIT, Angle.CHASE, Angle.TV2),
            (ShotKind.INCIDENT, ""): (Angle.TV1, Angle.TV3, Angle.CHOPPER),
            (ShotKind.INCIDENT, ShotFlavor.REPLAY): (Angle.TV1, Angle.TV2, Angle.TV3),
            (ShotKind.TROUBLE, ""): (Angle.TV1, Angle.CHOPPER, Angle.TV3),
            (ShotKind.FINISH, ""): (Angle.TV1, Angle.TV2, Angle.TV3),
            **_PIT_PROFILES,
        })

    @classmethod
    def cinematic(cls) -> AnglePolicy:
        """Widest range: weave chopper/blimp/scenic for drama and downtime."""
        return cls({
            (ShotKind.LEADER, ""): (Angle.TV1, Angle.CHOPPER, Angle.TV3, Angle.TV_MIXED),
            (ShotKind.LEADER, ShotFlavor.SOLO):
                (Angle.NOSE, Angle.CHOPPER, Angle.BLIMP, Angle.TV3),
            (ShotKind.FOLLOW, ""): (Angle.NOSE, Angle.CHOPPER, Angle.TV_MIXED, Angle.TV3),
            (ShotKind.BATTLE, ""): (Angle.TV2, Angle.CHASE, Angle.TV3, Angle.FAR_CHASE),
            (ShotKind.BATTLE, ShotFlavor.SIDE_BY_SIDE):
                (Angle.NOSE, Angle.CHASE, Angle.TV2, Angle.BLIMP),
            (ShotKind.BATTLE, ShotFlavor.PASS):
                (Angle.NOSE, Angle.CHASE, Angle.GEARBOX, Angle.FAR_CHASE),
            (ShotKind.INCIDENT, ""): (Angle.TV1, Angle.CHOPPER, Angle.TV3),
            (ShotKind.INCIDENT, ShotFlavor.REPLAY): (Angle.TV1, Angle.TV2, Angle.TV3),
            (ShotKind.TROUBLE, ""): (Angle.CHOPPER, Angle.TV1, Angle.BLIMP, Angle.TV3),
            (ShotKind.FINISH, ""): (Angle.TV1, Angle.TV2, Angle.TV3),
            **_PIT_PROFILES,
        })


# Registry so config can name a personality (from the settings file, the CLI or the
# control panel) without importing the classmethods directly.
#
# Each has a plain-language alias alongside its internal name, because "tv" is what
# someone picking a camera style would type and "onboard_forward" is what the person
# who wrote the profile would. Both resolve to the same policy, and the aliases are
# what the settings file and its documentation use.
POLICIES = {
    "classic": AnglePolicy.classic,
    "onboard_forward": AnglePolicy.onboard_forward,
    "cinematic": AnglePolicy.cinematic,
    # aliases
    "tv": AnglePolicy.classic,
    "onboard": AnglePolicy.onboard_forward,
    "wide": AnglePolicy.cinematic,
}


class AngleRotator:
    """Per-broadcast angle picker: holds rotation cursors and the last group used.

    One instance lives on an Actuator for the life of a broadcast; `pick` is called
    exactly once per cut, so the cursors advance per cut and give even variety
    within each (kind, flavor) while dodging back-to-back repeats of one group.
    """

    def __init__(self, policy: AnglePolicy | None = None):
        self.policy = policy or AnglePolicy.classic()
        self._cursor: dict[tuple[str, str], int] = {}
        self._last_group: int | None = None

    def pick(self, cmap: CameraMap, kind: str, flavor: str = "") -> int:
        """Resolve the next camera group for a shot; falls back to the map default
        when the track has none of the preferred angles."""
        ranked = self.policy.ranked(kind, flavor)
        avail = [name for name in ranked if cmap.available(name)]
        if not avail:
            self._last_group = cmap.default_group
            return cmap.default_group

        key = (kind, flavor)
        i = self._cursor.get(key, 0) % len(avail)
        group = cmap.resolve(avail[i])
        # avoid an identical-looking back-to-back cut when a real alternative exists
        if group == self._last_group and len(avail) > 1:
            i = (i + 1) % len(avail)
            group = cmap.resolve(avail[i])
        self._cursor[key] = i + 1
        self._last_group = group
        return group
