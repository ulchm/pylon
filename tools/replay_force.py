"""Run the live director with the replay bar dropped, to exercise the excursion on air.

The mode machine is deliberately picky: a MAJOR incident, a car in the top 20, and a
lull to leave on. That is right for a broadcast and useless for proving the code works
-- four minutes of clean mid-race running produced no candidate at all, so the part that
actually moves the tape (seek -> roll -> return, with the live world model starved for
the duration) has never run against a real sim.

This lowers the bar far enough that a replay fires within a minute or two, WITHOUT
touching what production does: it rewrites ReplayConfig's defaults in memory and then
hands off to the normal `pylon live` path.

It is a test rig. The thresholds here are not proposals: if replays feel too rare on
air, tune ReplayConfig, not this file.

    python tools/replay_force.py ws://simbox:8779
"""

from __future__ import annotations

import sys

from replay_why import instrument

from pylon.director import replay as R
from pylon.world import IncidentSeverity

# Every gate, opened as far as it goes. `safe_now` still requires a LEADER shot or a
# yellow, which is kept on purpose: cutting away mid-battle is the one behaviour that
# would be wrong even in a test.
LOOSE = {
    "min_severity": IncidentSeverity.MINOR,   # normally MAJOR
    "max_pos": 60,                            # normally 20; this field has 57 cars
    "lead_change_only_front": 10,             # normally 3
    "cooldown": 20.0,                         # normally 90
    "cooldown_tape": 20.0,                    # normally 45
    "max_age": 300.0,                         # normally 120
    "hold_for_lull": 120.0,                   # normally 25
}


def loosen() -> None:
    """Substitute a factory that applies LOOSE, and PROVE it took.

    The first version rewrote `ReplayConfig.__dataclass_fields__[k].default` and set the
    class attribute. Both are no-ops: a dataclass bakes its defaults into the generated
    `__init__` when the class is created, so afterwards neither is read. It printed
    "bar lowered" and changed nothing, and the run it produced was pure default config
    that happened to fire anyway. A test rig that lies about what it configured is worse
    than no test rig, so this one asserts.
    """
    import pylon.director as D

    orig = R.ReplayConfig

    def factory(**kw):
        return orig(**{**LOOSE, **kw})

    # cmd_live does `from .director import ... ReplayConfig ...` at CALL time, so the
    # package attribute is the one that matters; R is patched too for anything else.
    D.ReplayConfig = factory
    R.ReplayConfig = factory

    probe = D.ReplayConfig(enabled=True)
    for k, v in LOOSE.items():
        got = getattr(probe, k)
        if got != v:
            raise SystemExit(f"[force] patch FAILED: {k} is {got!r}, wanted {v!r}")
    print(f"[force] replay bar lowered and verified: {LOOSE}", flush=True)


if __name__ == "__main__":
    loosen()
    instrument()
    from pylon.cli import main
    sys.argv = ["pylon", "live", *sys.argv[1:], "--replays"]
    raise SystemExit(main())
