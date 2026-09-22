"""How the pieces of a live show are wired together, as functions the CLI calls.

These were the bodies of `pylon live`, `pylon broadcast` and `pylon bridge`: the
OBS switcher hook, the replay gate, the coupling between --replays and
interrupt_only_if_on_camera, which telemetry source to read and which command sink to
write. Inside an argparse handler none of that could be tested, and the coupling rules
were comments. Here each is a function that takes plain values and returns the object
the driver loop wants, and tests/test_wiring.py states the rules.

cli.py keeps the parser and the one-screen adapters: what gets printed, and when.
"""

from __future__ import annotations

from collections.abc import Callable

from ..camera import (
    POLICIES,
    Actuator,
    ActuatorConfig,
    CommandSink,
    DirectSink,
    LoggingController,
    WsCommandClient,
)
from ..director import DirectorConfig, ReplayConfig, ReplayDirector
from ..obs import SceneConfig
from ..telemetry import PlaybackSource
from ..telemetry.frame import SessionInfo

NO_SIM = "iRacing not detected (no live shared memory). Run on the Windows sim box."


# --- the brain ---------------------------------------------------------------

def director_config(*, replays: bool, **tuning) -> DirectorConfig:
    """The director's config for a live show.

    `interrupt_only_if_on_camera` is coupled to `replays` on purpose, and must stay
    coupled: on its own it would mean an off-camera crash is never shown at all. It is
    only safe to stop cutting live BECAUSE the replay will cover it properly.
    """
    return DirectorConfig(interrupt_only_if_on_camera=bool(replays), **tuning)


def replay_director(*, roll: float, lead_in: float,
                    slow_motion: bool = ReplayConfig.slow_at_moment) -> ReplayDirector:
    """The instant-replay machine: rolls the sim's own tape back over a crash.

    `roll` is how long the replay runs and `lead_in` how far before the moment it
    starts, so the viewer sees the run-up rather than the aftermath. Everything about
    WHEN it is safe to roll (a lull in the racing, a moment worth the cutaway, a
    contact that actually cost someone) is ReplayConfig, which the director owns.
    """
    return ReplayDirector(ReplayConfig(enabled=True, roll=roll, lead_in=lead_in,
                                       slow_at_moment=slow_motion))


def actuator_for(info: SessionInfo, angles: str) -> Actuator:
    """An Actuator whose angle personality is chosen by name (see camera/angles.py)."""
    return Actuator(info, ActuatorConfig(policy=POLICIES[angles]()))


# --- OBS ---------------------------------------------------------------------

def scene_config(*, post_race_hold: float | None = None, chequer_max_hold: float | None = None,
                 grid_cars: int | None = None, program: str | None = None,
                 pre: str | None = None, post: str | None = None,
                 post_nonrace: str | None = None) -> SceneConfig:
    """Scene names default to what `pylon obs-setup` builds. Only a name actually
    given overrides one, so an empty flag cannot blank a scene."""
    given: dict = {}
    if post_race_hold is not None:
        given["post_race_hold"] = post_race_hold
    if chequer_max_hold is not None:
        given["chequer_max_hold"] = chequer_max_hold
    if grid_cars is not None:
        given["grid_cars"] = grid_cars
    for name, val in (("program", program), ("pre", pre), ("post", post),
                      ("post_nonrace", post_nonrace)):
        if val:
            given[name] = val
    return SceneConfig(**given)


def obs_snapshot_hook(*, scene_cfg: SceneConfig, host: str, port: int,
                      password: str | None, transition: str,
                      say: Callable[[str], None] = print,
                      connect=None) -> Callable[[object], None] | None:
    """Wire the OBS switcher to the world's session state, best-effort.

    Returns the `on_snapshot` hook for the driver loop, or None when OBS is unreachable:
    the director then directs cameras only, which is the right degradation (a switcher
    failure must never take the camera down; see ObsSwitcher). Set up ONCE, not per
    bridge connection: a dropped bridge is our socket falling over, not the session
    restarting, so the switcher must not forget where the session had got to.

    `connect` is obs.connect unless injected.
    """
    from ..obs import ObsSwitcher, SceneDirector
    from ..obs import connect as obs_connect

    dial_with = connect or obs_connect

    def dial():
        return dial_with(host=host, port=port, password=password)

    try:
        client = dial()
    except Exception as e:  # noqa: BLE001 - never let OBS stop the director
        say(f"! OBS scenes requested but obs-websocket is unreachable at {host}:{port} "
            f"({e}); directing cameras only.")
        return None

    switcher = ObsSwitcher(client, transition=transition,
                           on_event=lambda m: say(f"            OBS      {m}"),
                           reconnect=dial)
    missing = switcher.check(scene_cfg)
    if missing:
        say(f"! OBS is missing {', '.join(missing)} - run `pylon obs-setup`.")
    scene_director = SceneDirector(scene_cfg)
    say(f"OBS scenes ON: {scene_cfg.pre} -> {scene_cfg.program} -> {scene_cfg.post} "
        f"(card {scene_cfg.post_race_hold:.0f}s into the cool-down)")

    def on_snapshot(snap) -> None:
        want = scene_director.update(snap)
        if want is not None:
            switcher.apply(want)

    return on_snapshot


# --- sources and sinks -------------------------------------------------------

def telemetry_source(*, live: bool, file: str | None, hz: float):
    """What a local director or a bridge reads: the sim's shared memory (Win32 only) or
    a recording. None when neither was asked for. Raises RuntimeError when the sim was
    asked for and is not there, with the message to print."""
    if live:
        from ..telemetry.live import LiveSource

        src = LiveSource(poll_hz=hz)
        if not src.connect() or not src.connected:
            raise RuntimeError(NO_SIM)
        return src
    if file:
        return PlaybackSource(file)
    return None


def camera_controller(*, sdk: bool):
    """The sim's cameras (pyirsdk, Win32 only) or a logger that only says what it would
    have done."""
    if sdk:
        from ..camera.sdk import SdkCameraController

        return SdkCameraController()
    return LoggingController()


def command_sink(*, bridge: str | None, sdk: bool) -> tuple[CommandSink, str]:
    """Where a local director's cuts go, and a one-line description of it."""
    if bridge:
        return WsCommandClient(bridge), f"bridge {bridge}"
    if sdk:
        return DirectSink(camera_controller(sdk=True)), "local iRacing (pyirsdk)"
    return DirectSink(LoggingController()), "log only (dry run)"
