"""The hand-off file: how the director tells the overlay what the camera is on.

They are separate processes, so one small JSON file carries it: written atomically
and read with a staleness bound, so a director that dies cannot pin the overlay to
the past.

  shot     director -> overlay    what the camera is on, plus the replay banner

The bridge also echoes the shot on its frame stream (BridgeClient.send_shot); the
overlay prefers a fresh file, then the echo. IPC only: the vocabulary the file carries
(shot kinds, flavors, subject keys) is contract.py, so both ends build the same strings
from one place.

The path is defined in settings.py and overridable with PYLON_SHOT_FILE; both
processes must agree.
"""

from __future__ import annotations

import json
import os
import time

from ..settings import SHOW

SHOT_PATH = SHOW.shot_file
STALE_AFTER = 15.0  # seconds; a shot file the director stopped updating is ignored


def write_shot(shot, replay: dict | None = None) -> None:
    """Publish the director's current shot (best-effort; never raises into the loop).

    `replay` carries the INSTANT-REPLAY state, and it is deliberately not the same thing
    as the model's `session.replay` badge. That badge comes from the sim's own
    IsReplayPlaying, which is true for every frame of a saved tape (#62), so it can
    never mean "we are showing you the past on purpose". This says the DIRECTOR took the
    broadcast somewhere else, which is the thing a viewer has to be told.

    `shot` may be None: the director has not cut yet (a replay can be armed before the
    opening shot). The file then carries only the replay block, and readers, which
    already treat a missing target as "no shot", see exactly that.
    """
    data = {
        "kind": shot.kind if shot is not None else None,
        "target": shot.target_idx if shot is not None else None,
        "pair": list(shot.pair) if shot is not None and shot.pair else None,
        "replay": replay,
        # The director's telemetry-derived angle hint (ShotFlavor): it tells a leader
        # running free from one with company, which the overlay cannot see itself.
        # Readers must treat it as optional: the bridge echo does not carry it yet.
        "flavor": shot.flavor if shot is not None else None,
        "t": time.time(),
    }
    try:
        tmp = SHOT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, SHOT_PATH)   # atomic swap so the reader never sees a half file
    except OSError:
        pass


# The last record that read cleanly, and when. The director rewrites the file EVERY
# FRAME while a replay rolls (write a tmp, os.replace it in), and on Windows a read
# that lands inside that swap fails. Seen on air 2026-09-20, Round 1: one failed read
# per excursion, the overlay fell back to the bridge echo, which carries no replay
# banner, and the page wiped out of the replay and straight back in, mid-tape. A file
# that read fine a moment ago has not changed meaning in one frame, so a failed read
# inside READ_GRACE answers with the last good record. Staleness is a different thing
# (the director has stopped writing) and still answers None.
_last_good: tuple[float, dict] | None = None
READ_GRACE = 1.0


def read_shot() -> dict | None:
    """The director's current shot as {kind,target,pair}, or None if stale/absent."""
    global _last_good
    try:
        if time.time() - SHOT_PATH.stat().st_mtime > STALE_AFTER:
            _last_good = None
            return None
        data = json.loads(SHOT_PATH.read_text())
    except (OSError, ValueError):
        if _last_good is not None and time.time() - _last_good[0] < READ_GRACE:
            return _last_good[1]
        return None
    _last_good = (time.time(), data)
    return data
