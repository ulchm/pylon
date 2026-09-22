"""`pylon doctor`: the first thing to run when something is wrong.

What these guard is the difference between a diagnosis and an instruction. A check
that says "obs-websocket handshake failed" has told a developer something and told
everyone else nothing, so every failing check here is asserted to carry the ONE next
action, and the actions for the three ways OBS can be unreachable are asserted to be
three different actions.
"""

from __future__ import annotations

import pytest

from pylon.config import AdvancedConfig, Config, DirectorConfigFile, ObsConfig
from pylon.settings import ShowSettings
from pylon.show import doctor


class FakeObs:
    """Enough obs-websocket to answer the doctor's questions."""

    def __init__(self, scenes=(), transitions=("Fade", "Cut"), version="31.0.0"):
        self._scenes = list(scenes)
        self._transitions = list(transitions)
        self._version = version

    def get_version(self):
        return type("V", (), {"obs_version": self._version})()

    def get_scene_list(self):
        return type("S", (), {"scenes": [{"sceneName": n} for n in self._scenes]})()

    def get_scene_transition_list(self):
        return type("T", (), {"transitions": [{"transitionName": n}
                                              for n in self._transitions]})()


def _connect(client):
    def dial(**kw):
        if client is None:
            raise OSError("Connection refused")
        return client
    return dial


def _run(cfg=None, client=None, **kw):
    return doctor.run(cfg or Config(), connect=_connect(client), check_sim=False, **kw)


def _state(rep, name):
    return next(c for c in rep.checks if c.name == name)


# --- OBS, and the three different things that are wrong -----------------------

def test_obs_unreachable_is_a_failure_and_the_only_one_that_stops_a_broadcast():
    rep = _run()
    assert _state(rep, "OBS").state == doctor.BAD
    assert rep.ok is False


def test_a_password_that_was_set_by_hand_is_blamed_before_obs_is(monkeypatch):
    """Three causes, three actions. Someone who typed a password into the settings
    must be sent to the password, not told to enable a server they already enabled."""
    cfg = Config(obs=ObsConfig(password="hunter2"))
    rep = _run(cfg)
    assert "password" in _state(rep, "OBS").fix


def test_a_websocket_that_was_never_enabled_is_told_where_to_enable_it(monkeypatch, tmp_path):
    """The install that has never had obs-websocket switched on. The password file
    OBS writes for itself is what separates this from "OBS is simply closed", and
    the two need different instructions."""
    from pylon.obs import setup as obs_setup

    monkeypatch.setattr(obs_setup, "WS_CONFIG", tmp_path / "never-written.json")
    rep = _run()
    assert "WebSocket Server Settings" in _state(rep, "OBS").fix


def test_a_websocket_that_is_set_up_but_closed_is_told_to_start_obs(monkeypatch, tmp_path):
    from pylon.obs import setup as obs_setup

    ws = tmp_path / "config.json"
    ws.write_text('{"server_password": "abc123"}')
    monkeypatch.setattr(obs_setup, "WS_CONFIG", ws)
    monkeypatch.setattr(obs_setup, "default_password", lambda: "abc123")
    rep = _run()
    assert "start OBS" in _state(rep, "OBS").fix


def test_obs_reached_reports_its_version_and_says_nothing_to_fix():
    rep = _run(client=FakeObs(scenes=["iRacing - Broadcast", "Pylon - Starting Soon",
                                      "Pylon - Intermission", "Pylon - Race Complete"]))
    obs = _state(rep, "OBS")
    assert obs.state == doctor.OK and "31.0.0" in obs.detail and obs.fix == ""


# --- the scenes ----------------------------------------------------------------

def test_missing_scenes_are_a_note_with_the_one_button_that_makes_them():
    """Not a failure: the fix is one click, and a first run has none of them."""
    rep = _run(client=FakeObs(scenes=["iRacing - Broadcast"]))
    scenes = _state(rep, "Scenes")
    assert scenes.state == doctor.WARN
    assert "Set up OBS" in scenes.fix
    assert "Starting Soon" in scenes.detail


def test_the_scenes_are_looked_for_under_the_shows_own_name():
    """Two broadcasters on one PC must not collide, so EVERY scene carries the show's
    tag, the programme scene included. The doctor has to look for the same names
    obs-setup builds, or it reports every scene missing on a perfectly good install."""
    from pylon.config import ShowConfig

    cfg = Config(show=ShowConfig(name="Thursday Night Racing", tag="TNR"))
    rep = _run(cfg, client=FakeObs(scenes=["TNR - Broadcast", "TNR - Starting Soon",
                                           "TNR - Intermission", "TNR - Race Complete"]))
    assert _state(rep, "Scenes").state == doctor.OK


def test_a_named_show_never_looks_at_the_unnamed_programme_scene():
    """The failure this stops: `obs-setup` reaching into the programme scene of
    another broadcaster already installed on the same PC."""
    from pylon.config import ShowConfig
    from pylon.show.obssetup import program_scene

    assert program_scene(Config(show=ShowConfig(tag="TNR"))) == "TNR - Broadcast"
    assert program_scene(Config(show=ShowConfig(name="Thursday Night Racing"))) \
        == "Thursday Night Racing - Broadcast"
    # ...and a PC with one broadcaster on it gets the plain name.
    assert program_scene(Config()) == "iRacing - Broadcast"


def test_a_transition_obs_does_not_have_is_reported_before_it_fails_silently():
    """A scene switch naming a transition OBS has never heard of is the kind of
    failure that only shows up as a cut that did not happen, mid-race."""
    cfg = Config(obs=ObsConfig(transition="GOW Slam"))
    rep = _run(cfg, client=FakeObs(scenes=["iRacing - Broadcast", "Pylon - Starting Soon",
                                           "Pylon - Intermission", "Pylon - Race Complete"]))
    t = _state(rep, "Transition")
    assert t.state == doctor.WARN and "Fade" in t.fix


def test_no_transition_check_when_pylon_is_not_switching_scenes():
    cfg = Config(obs=ObsConfig(scenes=False, transition="Nonexistent"))
    rep = _run(cfg, client=FakeObs(scenes=["iRacing - Broadcast", "Pylon - Starting Soon",
                                           "Pylon - Intermission", "Pylon - Race Complete"]))
    assert not [c for c in rep.checks if c.name == "Transition"]


# --- the settings themselves ----------------------------------------------------

def test_a_settings_problem_is_surfaced_here_rather_than_only_in_a_log():
    cfg = Config()
    cfg.problems.append("look.colour: expected text, got 5")
    rep = _run(cfg)
    s = _state(rep, "Settings")
    assert s.state == doctor.WARN and "look.colour" in s.detail


def test_a_camera_style_that_does_not_exist_is_reported_with_the_ones_that_do():
    cfg = Config(director=DirectorConfigFile(angles="helicopter-only"))
    rep = _run(cfg)
    c = _state(rep, "Camera style")
    assert c.state == doctor.WARN
    assert '"tv"' in c.fix and '"onboard"' in c.fix


# --- ports ----------------------------------------------------------------------

def test_a_port_already_held_is_named_and_blamed_on_the_likely_cause():
    """Almost always a second copy of Pylon, whose workers would now be doubled."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    held = s.getsockname()[1]
    try:
        settings = ShowSettings.from_config(Config(advanced=AdvancedConfig(bridge_port=held)))
        rep = _run(settings=settings)
        ports = _state(rep, "Ports")
        assert ports.state == doctor.WARN
        assert str(held) in ports.detail
        assert "already running" in ports.fix
    finally:
        s.close()


def test_free_ports_say_so_and_nothing_else():
    rep = _run()
    assert _state(rep, "Ports").state == doctor.OK


# --- the report -----------------------------------------------------------------

def test_a_warning_is_not_a_failure():
    """Most warnings are "iRacing is not running", which is true most of the time and
    must not read as a broken install."""
    rep = _run(client=FakeObs(scenes=["iRacing - Broadcast"]))
    assert any(c.state == doctor.WARN for c in rep.checks)
    assert rep.ok is True


def test_every_failing_check_carries_exactly_one_next_action():
    """The whole point: a diagnosis helps a developer, an instruction helps everyone."""
    rep = _run()
    for c in rep.checks:
        if c.state in (doctor.BAD, doctor.WARN):
            assert c.fix, f"{c.name} says what is wrong and not what to do"


def test_the_lines_print_the_fix_under_the_check_it_belongs_to():
    rep = _run()
    lines = rep.lines()
    bad = next(i for i, ln in enumerate(lines) if "FAIL" in ln and "OBS" in ln)
    assert lines[bad + 1].strip().startswith("->")


@pytest.mark.parametrize("checker", [doctor.run])
def test_the_doctor_never_raises_whatever_obs_does(checker):
    """It is the thing people run when something is already wrong."""
    class Exploding:
        def get_version(self):
            raise RuntimeError("boom")

        def get_scene_list(self):
            raise RuntimeError("boom")

    rep = checker(Config(), connect=_connect(Exploding()), check_sim=False)
    assert _state(rep, "Scenes").state == doctor.BAD
