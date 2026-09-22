"""iRacing telemetry enum values and constants (from the SDK).

Only the subset we use. Values match the iRacing SDK so recordings and live data
share one vocabulary.
"""

from __future__ import annotations

# Fixed length of every CarIdx* array in iRacing telemetry.
MAX_CARS = 64


class TrackSurface:
    """irsdk_TrkLoc: value of CarIdxTrackSurface / PlayerTrackSurface."""

    NOT_IN_WORLD = -1
    OFF_TRACK = 0
    IN_PIT_STALL = 1
    APPROACHING_PITS = 2
    ON_TRACK = 3


class SessionState:
    """irsdk_SessionState."""

    INVALID = 0
    GET_IN_CAR = 1
    WARMUP = 2
    PARADE_LAPS = 3
    RACING = 4
    CHECKERED = 5
    COOL_DOWN = 6


class SessionFlag:
    """irsdk_Flags bitfield (subset). SessionFlags is an OR of these."""

    CHECKERED = 0x00000001
    WHITE = 0x00000002
    GREEN = 0x00000004
    YELLOW = 0x00000008
    RED = 0x00000010
    BLUE = 0x00000020
    DEBRIS = 0x00000040
    CROSSED = 0x00000080
    YELLOW_WAVING = 0x00000100
    ONE_LAP_TO_GREEN = 0x00000200
    GREEN_HELD = 0x00000400
    TEN_TO_GO = 0x00000800
    FIVE_TO_GO = 0x00001000
    RANDOM_WAVING = 0x00002000
    CAUTION = 0x00004000
    CAUTION_WAVING = 0x00008000

    # ---- the "drivers black flags" group ----
    # These are shown to ONE car rather than to the field, so they are what
    # CarIdxSessionFlags is for. The global SessionFlags never carries them.
    BLACK = 0x00010000            # a penalty: serve it or be disqualified
    DISQUALIFY = 0x00020000
    # NOT A FLAG. The SDK's own comment is "car is allowed service", and it is set on
    # essentially every car for essentially the whole session: measured on capture2:
    # 119,994 car-frames of it, against 115 of a real meatball and 3 of a furled black.
    # Anything that treats "CarIdxSessionFlags != 0" as "this car is flagged" therefore
    # flags the entire field for the entire race. Mask it off. This is the same shape as
    # the trap the global flags already document (DESIGN.md 14): normal green-flag
    # running carries only background bits.
    SERVICIBLE = 0x00040000
    FURLED = 0x00080000           # a rolled-up black flag: a warning, not yet a penalty
    REPAIR = 0x00100000           # the MEATBALL: dangerous damage, come in and fix it
    DQ_SCORING_INVALID = 0x00200000   # also sets DISQUALIFY


# The bits in CarIdxSessionFlags that are worth putting on a broadcast: driver-directed
# orders and penalties, and nothing else. SERVICIBLE is excluded because it is not a
# flag at all (see above), and BLUE because being lapped is not a penalty: in a race
# with any spread it would light up most of the field most of the time, which is noise
# on a timing tower rather than information.
DRIVER_FLAG_MASK = (
    SessionFlag.BLACK
    | SessionFlag.DISQUALIFY
    | SessionFlag.DQ_SCORING_INVALID
    | SessionFlag.FURLED
    | SessionFlag.REPAIR
)
