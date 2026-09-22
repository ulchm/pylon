"""The CLI is the seam every worker crosses: the studio builds a command line, and the
worker parses it. Nothing else in the suite exercised that seam, and the failure it
guards is quiet. A worker that rejects its own argv exits at startup; the studio retries
it five times with a backoff and gives up; the only explanation is argparse's two-line
complaint at the top of a worker log nobody opens. So every command line the studio can
emit is parsed here, under the parser that will receive it.

The second thing here is where the defaults come from. Wiring (ports, hosts, URLs) reads
from ShowSettings; tuning (shot lengths, replay timing, the chequer hold) reads from the
dataclass that consumes it. Neither is retyped in an argparse block any more, and these
tests are what keeps it that way.
"""

from __future__ import annotations

import pytest

from pylon.camera.angles import POLICIES
from pylon.cli import ANGLE_CHOICES, build_parser
from pylon.director import DirectorConfig, ReplayConfig
from pylon.obs import SceneConfig
from pylon.settings import SHOW, ShowSettings
from pylon.show.studio import default_workers

# which parsed attribute is the port a worker BINDS (None: it only dials out)
BOUND_PORT = {"bridge": "port", "overlay": "http_port", "director": None}
COMMAND_OF = {"bridge": "cmd_bridge", "overlay": "cmd_overlay", "director": "cmd_live"}


def _parse(parser, argv):
    """parse_args exits the process on a bad flag. Turn that into a failure that shows
    the command line, which is the thing worth reading."""
    try:
        return parser.parse_args(list(argv))
    except SystemExit as e:
        pytest.fail(f"{list(argv)!r} does not parse under build_parser (exit {e.code})")


def _cli_workers(**kw):
    """(worker, argv-after-the-interpreter) for every worker the studio spawns."""
    out = []
    for w in default_workers(python="PY", **kw):
        assert list(w.argv[:3]) == ["PY", "-m", "pylon"], w.argv
        out.append((w, list(w.argv[3:])))
    assert [w.name for w, _ in out] == ["bridge", "overlay", "director"]
    return out


# --- the studio's command lines -------------------------------------------------

def test_every_argv_the_studio_emits_parses_under_the_cli():
    parser = build_parser()
    for w, argv in _cli_workers(replays=True, obs_scenes=True):
        args = _parse(parser, argv)
        assert args.func.__name__ == COMMAND_OF[w.name], (w.name, argv)


def test_the_replay_free_studio_parses_too():
    """The flags come and go with the studio's options; both shapes have to parse."""
    parser = build_parser()
    for w, argv in _cli_workers(replays=False, obs_scenes=False, replay="rec.jsonl.gz"):
        _parse(parser, argv)


def test_the_studio_probes_the_port_each_worker_is_told_to_bind():
    """The ready check dials `Worker.port`; the worker binds whatever its argv says. Both
    come from the same ShowSettings, and this is the proof they meet at one number,
    including when the settings are not the defaults."""
    custom = ShowSettings(bridge_port=9001, overlay_http_port=9002, overlay_ws_port=9003)
    parser = build_parser()
    for w, argv in _cli_workers(settings=custom):
        args = _parse(parser, argv)
        attr = BOUND_PORT[w.name]
        bound = None if attr is None else getattr(args, attr)
        assert bound == w.port, (w.name, bound, w.port)


def test_everything_downstream_dials_the_bridge_the_studio_started():
    custom = ShowSettings(bridge_port=9001)
    parser = build_parser()
    for w, argv in _cli_workers(settings=custom):
        args = _parse(parser, argv)
        if w.name == "bridge":
            continue
        dialled = args.url if w.name == "director" else args.bridge
        assert dialled == "ws://127.0.0.1:9001", (w.name, dialled)


def test_the_overlay_serves_the_websocket_the_page_will_dial():
    """The overlay worker binds two ports. OBS's browser source loads the page off one
    and the page dials the other, so both have to be the ones in the page URL."""
    parser = build_parser()
    overlay = next(argv for w, argv in _cli_workers() if w.name == "overlay")
    args = _parse(parser, overlay)
    assert f"http://localhost:{args.http_port}/" in SHOW.overlay_page_url()
    assert f"ws://localhost:{args.ws_port}&live" in SHOW.overlay_page_url()


# --- a worker started by hand ---------------------------------------------------

def test_a_worker_run_by_hand_binds_where_the_studio_would_probe():
    """`pylon overlay --bridge ...` typed at a prompt with no port flags must land on
    the port the studio expects: its reclaim of a hand-run copy depends on it, and so
    does every URL obs-setup writes into OBS."""
    parser = build_parser()
    ports = {w.name: w.port for w in default_workers(python="PY")}
    assert _parse(parser, ["bridge"]).port == ports["bridge"]
    assert _parse(parser, ["overlay", "--bridge", "ws://x"]).http_port == ports["overlay"]
    studio = _parse(parser, ["studio"])
    assert (studio.control_host, studio.control_port) == (SHOW.loopback, SHOW.control_port)
    assert studio.bridge_port == ports["bridge"]


# --- where the defaults come from ----------------------------------------------

def test_director_tuning_defaults_are_the_dataclass_defaults():
    """Change DirectorConfig.max_shot and `pylon live` must follow. It did not: the
    numbers were retyped in three argparse blocks, so a change to the dataclass moved
    the tests while every CLI path kept forcing the old value back."""
    d = DirectorConfig()
    for cmd in (["live", "ws://x"], ["broadcast", "rec.jsonl.gz"], ["direct", "rec.jsonl.gz"]):
        a = _parse(build_parser(), cmd)
        assert (a.min_shot, a.max_shot, a.cut_margin) == (d.min_shot, d.max_shot, d.cut_margin)


def test_replay_and_scene_defaults_are_the_dataclass_defaults():
    r, s = ReplayConfig(), SceneConfig()
    live = _parse(build_parser(), ["live", "ws://x"])
    assert (live.replay_roll, live.replay_lead_in) == (r.roll, r.lead_in)
    assert live.post_race_hold == s.post_race_hold
    assert live.chequer_max_hold == s.chequer_max_hold
    assert live.grid_cars == s.grid_cars
    assert live.scene_transition == SHOW.scene_transition
    assert (live.obs_host, live.obs_port) == (SHOW.obs_host, SHOW.obs_port)


def test_obs_setup_points_at_the_pages_the_overlay_worker_serves():
    a = _parse(build_parser(), ["obs-setup"])
    assert a.overlay_url == SHOW.overlay_page_url()
    assert a.cards_url == SHOW.cards_url()
    assert (a.host, a.port) == (SHOW.obs_host, SHOW.obs_port)
    assert (a.width, a.height, a.fps) == (*SHOW.canvas, SHOW.fps)


def test_the_live_poll_rate_is_one_number():
    hz = SHOW.live_hz
    assert _parse(build_parser(), ["bridge"]).hz == hz
    assert _parse(build_parser(), ["record", "out.jsonl.gz"]).hz == hz
    assert _parse(build_parser(), ["broadcast", "--live"]).hz == hz


def test_every_camera_style_offered_is_a_policy_that_exists():
    """`--angles` is validated against a tuple typed in cli.py; the policies live in
    camera/angles.py. A style offered on the menu and missing from the registry is a
    crash at the first cut, which is the worst possible moment to find out.

    A subset, not an equality: POLICIES also holds the internal names, which still
    work but are not offered, because a menu with two names for one thing is worse.
    """
    assert set(ANGLE_CHOICES) <= set(POLICIES)
    assert set(ANGLE_CHOICES) == {"tv", "onboard", "wide"}


def test_the_settings_file_decides_the_camera_style_and_a_bad_one_falls_back():
    """A typo in a settings file must not stop a broadcast: `pylon doctor` reports it
    and the show goes on with the default."""
    from pylon import cli
    from pylon.config import Config, DirectorConfigFile

    real = cli._cfg
    try:
        cli._cfg = Config(director=DirectorConfigFile(angles="wide"))
        assert cli._angles_default() == "wide"
        cli._cfg = Config(director=DirectorConfigFile(angles="Onboard"))
        assert cli._angles_default() == "onboard", "case is not something to fail over"
        cli._cfg = Config(director=DirectorConfigFile(angles="helicopter-only"))
        assert cli._angles_default() == "tv"
    finally:
        cli._cfg = real
