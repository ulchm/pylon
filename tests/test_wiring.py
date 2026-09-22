"""The rules for wiring a live show, stated as tests.

These rules used to be comments inside `pylon live`'s argparse handler, where nothing
could check them: that --replays also stops the director cutting live to an off-camera
crash, that the replay machine carries the timings it is given, that an unreachable OBS
leaves the director directing cameras rather than dead, that a scene flag left empty
does not blank a scene. show/wiring.py makes each a function; this is where the rules
are written down as things that fail.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pylon.camera import Actuator, DirectSink, LoggingController, WsCommandClient
from pylon.director import DirectorConfig
from pylon.obs import SceneConfig
from pylon.obs.setup import PROGRAM_SCENE, SCENE_PREFIX
from pylon.show import wiring
from pylon.telemetry.constants import SessionState
from pylon.telemetry.frame import SessionInfo
from pylon.world.model import SessionKind, SessionSnapshot

PRE = f"{SCENE_PREFIX}Starting Soon"
POST = f"{SCENE_PREFIX}Race Complete"
MID = f"{SCENE_PREFIX}Intermission"


# --- the brain ------------------------------------------------------------------

def test_replays_on_means_an_off_camera_incident_is_left_to_the_replay():
    """Coupled on purpose. On its own, interrupt_only_if_on_camera would mean an
    off-camera crash is never shown at all; it is only safe BECAUSE the replay covers it."""
    assert wiring.director_config(replays=True).interrupt_only_if_on_camera is True
    assert wiring.director_config(replays=False).interrupt_only_if_on_camera is False


def test_tuning_passes_through_and_everything_else_keeps_its_default():
    cfg = wiring.director_config(replays=False, min_shot=1.0, max_shot=2.0, cut_margin=0.5)
    assert (cfg.min_shot, cfg.max_shot, cfg.cut_margin) == (1.0, 2.0, 0.5)
    assert cfg.battle_max_shot == DirectorConfig().battle_max_shot


def test_the_replay_machine_is_enabled_and_carries_the_timings_it_was_given():
    r = wiring.replay_director(roll=9.0, lead_in=6.0)
    assert r.cfg.enabled
    assert (r.cfg.roll, r.cfg.lead_in) == (9.0, 6.0)


def test_the_actuator_is_built_for_the_named_angle_personality():
    assert isinstance(wiring.actuator_for(SessionInfo({}), "cinematic"), Actuator)
    with pytest.raises(KeyError):
        wiring.actuator_for(SessionInfo({}), "handheld")


# --- OBS ------------------------------------------------------------------------

def test_scene_names_override_only_when_actually_given():
    d = SceneConfig()
    cfg = wiring.scene_config(post_race_hold=12.0, program="Live", pre=None, post="",
                              post_nonrace=None)
    assert (cfg.program, cfg.pre, cfg.post, cfg.post_nonrace) == ("Live", d.pre, d.post,
                                                                   d.post_nonrace)
    assert cfg.post_race_hold == 12.0
    assert wiring.scene_config() == SceneConfig()


class FakeOBS:
    def __init__(self, scenes=(PROGRAM_SCENE, PRE, POST, MID), transitions=("GOW Slam",)):
        self._scenes, self._transitions = scenes, transitions
        self.calls = []

    def get_scene_list(self):
        return SimpleNamespace(scenes=[{"sceneName": s} for s in self._scenes])

    def get_scene_transition_list(self):
        return SimpleNamespace(transitions=[{"transitionName": t} for t in self._transitions])

    def set_current_scene_transition(self, name):
        self.calls.append(("transition", name))

    def set_current_program_scene(self, name):
        self.calls.append(("scene", name))


def _world(t, state, kind=SessionKind.UNKNOWN):
    """A WorldSnapshot as the hook sees it: the session, the clock, and no cars."""
    return SimpleNamespace(session_time=t, cars={}, session=SessionSnapshot(
        session_time=t, flags=0, state=state,
        is_green=state == SessionState.RACING, is_yellow=False, is_red=False,
        is_checkered=state == SessionState.CHECKERED,
        time_remaining=None, laps_remaining=None, is_last_lap=False,
        event_type="Race", session_kind=kind))


def _hook(fake, cfg=None, said=None, connect=None):
    return wiring.obs_snapshot_hook(
        scene_cfg=cfg or SceneConfig(settle=1.0), host="h", port=1, password=None,
        transition="GOW Slam", say=(said if said is not None else []).append,
        connect=connect or (lambda **kw: fake))


def test_unreachable_obs_means_cameras_only_not_a_dead_director():
    said = []

    def refused(**kw):
        raise OSError("connection refused")

    assert _hook(None, said=said, connect=refused) is None
    assert any("directing cameras only" in s for s in said)


def test_the_hook_cuts_scenes_from_the_sessions_state_with_the_house_transition():
    fake = FakeOBS()
    hook = _hook(fake)
    assert hook is not None
    for t in range(6):
        hook(_world(float(t), SessionState.GET_IN_CAR))
    assert fake.calls == [("transition", "GOW Slam"), ("scene", PRE)]
    for t in range(6, 12):
        hook(_world(float(t), SessionState.RACING))
    assert fake.calls[-1] == ("scene", PROGRAM_SCENE)


def test_a_missing_scene_is_named_at_startup_not_discovered_at_the_chequer():
    said = []
    hook = _hook(FakeOBS(scenes=(PROGRAM_SCENE,)), said=said)
    assert hook is not None                       # still worth running for the programme
    warning = next(s for s in said if "OBS is missing" in s)
    assert PRE in warning and POST in warning and "obs-setup" in warning


def test_the_hook_says_what_it_will_do():
    said = []
    _hook(FakeOBS(), cfg=SceneConfig(post_race_hold=45.0), said=said)
    assert any(s.startswith("OBS scenes ON:") and "45s" in s for s in said)


# --- sources and sinks ----------------------------------------------------------

def test_no_source_asked_for_is_none_not_an_error():
    """`pylon bridge` with neither --live nor --replay serves camera commands only."""
    assert wiring.telemetry_source(live=False, file=None, hz=10.0) is None


def test_the_dry_run_sink_only_logs():
    sink, where = wiring.command_sink(bridge=None, sdk=False)
    assert isinstance(sink, DirectSink) and isinstance(sink.controller, LoggingController)
    assert "dry run" in where


def test_a_bridge_url_makes_a_remote_sink_without_dialling_yet():
    sink, where = wiring.command_sink(bridge="ws://nowhere:1", sdk=False)
    assert isinstance(sink, WsCommandClient) and sink.url == "ws://nowhere:1"
    assert "ws://nowhere:1" in where


def test_the_logging_controller_is_the_default_when_the_sdk_is_not_asked_for():
    assert isinstance(wiring.camera_controller(sdk=False), LoggingController)
