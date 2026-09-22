"""The vocabulary that crosses process boundaries.

The director names what the camera is on and the overlay draws a pop-in for it. Those
are separate processes, and the strings they pass through the hand-off file
(shotlink.py) have to agree character for character. They used to be built
independently and merely happen to match.

Nothing here imports anything, on purpose. camera/ needs ShotKind and ShotFlavor to
pick an angle, and importing them from director.model used to execute the whole
director package, and the world model behind it, to read a handful of constants. That
is what made "the bridge transport imports without the brain" false at import time.
"""

from __future__ import annotations


class ShotKind:
    LEADER = "leader"
    BATTLE = "battle"
    FOLLOW = "follow"
    INCIDENT = "incident"
    TROUBLE = "trouble"   # a car crashed / stranded / limping: stay with it
    FINISH = "finish"     # a car coming to the line under the chequer, or just across it:
                          # the flag for the winner, then each finisher in turn. Angle
                          # policies without a profile for it get the trackside TV set,
                          # which is where a finish is shot from anyway
    PIT = "pit"           # a car that DROVE into the lane, from the entry through the box:
                          # a pit stop, held while it lasts. The way back out is a FOLLOW
                          # with the REJOIN flavor, because the lane cameras cannot see the
                          # rejoin and a second cut is what gets the camera out there


class ShotFlavor:
    """Extra situational context the director derives from telemetry so the render
    side (the actuator's angle policy) can pick a fitting camera. The director owns
    this because only it sees the live world; the actuator sees only static session
    info. An empty flavor ("") means "no special context, use the kind's default"."""

    SOLO = "solo"          # leader running free: nobody within reach behind (in-car shines)
    SIDE_BY_SIDE = "sbs"   # battle pair overlapping / nose-to-tail (chase & nose frame it best)
    PASS = "pass"          # a place just changed hands in the pair: hold on it, framed from
                           # the car that LOST the place looking FORWARD (chase / nose /
                           # cockpit), so the winner is seen coming back across in front of
                           # it. The shot is only worth anything from behind the pass: on the
                           # winner it is a car driving away from an empty road
    REJOIN = "rejoin"      # a car coming out of the pit lane: the FOLLOW that ends a PIT
                           # shot, wanting a camera that shows the road it is merging
                           # into and who is on it, not the lane it is leaving
    REPLAY = "replay"      # the look for an instant replay: the tape is elsewhere and the
                           # picture has to READ as a replay, which means the trackside TV
                           # cameras a viewer knows replays from. Never the chopper: from
                           # up there a two-car touch is two dots, and it went out on air
                           # (2026-09-14) as "wrong camera"


def subject_of(kind: str, target: int, pair=None) -> str:
    """What the camera is ON, as one string.

    Both sides build it and they have to agree character for character. The director
    names its shots with it (director.core.candidates) and anything reading the shot
    file derives the same string from it to tell whether two shots are about the same
    thing, which only works if the two are built the same way.

    Pair order is normalised, so a pass INSIDE the pair, which reorders it, is the same
    subject rather than a new one.
    """
    if pair:
        a, b = sorted(pair)
        return f"{kind}:{a}:{b}"
    return f"{kind}:{target}"
