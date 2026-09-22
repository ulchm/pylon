"""`pylon doctor`: check everything a broadcast needs, and say what to do about it.

This is the first thing to run when something is wrong, and it is written for
someone who has never opened a terminal before today. Every check answers in one
line, and every failing check says the ONE next action, not a diagnosis.

The order is the order things depend on each other, so the first failure is
usually the only real one: a settings file, then OBS, then the scenes inside it,
then iRacing, then the ports Pylon wants to bind.

Nothing here changes anything. `pylon obs-setup` is the fixer; this only looks.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import dataclass, field

from ..config import Config, config_path
from ..config import load as load_config
from ..settings import SHOW, ShowSettings

OK, WARN, BAD = "ok", "warn", "bad"


@dataclass
class Check:
    name: str
    state: str          # OK / WARN / BAD
    detail: str = ""
    fix: str = ""       # the one next action, when there is one


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(name, state, detail, fix))

    @property
    def ok(self) -> bool:
        """True when nothing is actually broken. A warning is not a failure: most
        of them are "iRacing is not running", which is true most of the time."""
        return not any(c.state == BAD for c in self.checks)

    def lines(self) -> list[str]:
        mark = {OK: "  ok  ", WARN: " note ", BAD: " FAIL "}
        out = []
        for c in self.checks:
            out.append(f"[{mark[c.state]}] {c.name}" + (f": {c.detail}" if c.detail else ""))
            if c.fix:
                out.append(f"           -> {c.fix}")
        return out


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can we bind it? Not "is something answering": a port held by a dead worker's
    orphan answers nothing and still stops the replacement binding, which is the
    failure this is here to catch."""
    s = socket.socket()
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _check_config(rep: Report, cfg: Config) -> None:
    path = config_path()
    if cfg.problems:
        rep.add("Settings", WARN, "; ".join(cfg.problems),
                f"edit {path}, or fix it in the control panel's Settings tab")
    elif path.exists():
        rep.add("Settings", OK, str(path))
    else:
        rep.add("Settings", OK, "none yet; the defaults are a working show")


def _check_angles(rep: Report, cfg: Config) -> None:
    from ..camera import POLICIES

    want = cfg.director.angles.strip().lower()
    if want in POLICIES:
        rep.add("Camera style", OK, want)
    else:
        rep.add("Camera style", WARN, f"{want!r} is not one this version has",
                'set director.angles to "tv", "onboard" or "wide"')


def _check_obs(rep: Report, cfg: Config, settings: ShowSettings, connect=None) -> object | None:
    """Reach OBS, and say which of the three usual things is wrong when we cannot."""
    from ..obs import connect as obs_connect
    from ..obs import default_password
    from ..obs.setup import WS_CONFIG

    dial = connect or obs_connect
    try:
        cl = dial(host=cfg.obs.host, port=cfg.obs.port,
                  password=cfg.obs.password or None, timeout=3.0)
    except Exception as e:  # noqa: BLE001 - every failure here is a message, not a trace
        # Three causes, and they need different actions from the operator. The
        # password file existing tells us OBS has at least had its WebSocket server
        # switched on at some point, which separates "not enabled" from "not running".
        if cfg.obs.password:
            fix = "check the password in Settings, or clear it to read it from OBS"
        elif WS_CONFIG.exists() and default_password():
            fix = "start OBS (its WebSocket server is set up, so this is just OBS being closed)"
        else:
            fix = "in OBS: Tools > WebSocket Server Settings > Enable WebSocket server"
        rep.add("OBS", BAD, f"cannot reach {cfg.obs.host}:{cfg.obs.port} ({e})", fix)
        return None

    try:
        version = cl.get_version().obs_version
    except Exception:  # noqa: BLE001
        version = "?"
    rep.add("OBS", OK, f"connected, version {version}")
    return cl


def _check_scenes(rep: Report, cl, cfg: Config) -> None:
    from ..show.obssetup import program_scene, scene_prefix

    try:
        have = {s["sceneName"] for s in cl.get_scene_list().scenes}
    except Exception as e:  # noqa: BLE001
        rep.add("Scenes", BAD, f"could not read the scene list ({e})", "restart OBS")
        return
    prefix = scene_prefix(cfg)
    want = [program_scene(cfg), f"{prefix}Starting Soon", f"{prefix}Intermission",
            f"{prefix}Race Complete"]
    missing = [w for w in want if w not in have]
    if not missing:
        rep.add("Scenes", OK, f"{len(want)} present")
    else:
        rep.add("Scenes", WARN, f"missing {', '.join(missing)}",
                'click "Set up OBS" in the control panel, or run: pylon obs-setup')

    if cfg.obs.scenes:
        try:
            transitions = {t["transitionName"] for t in
                           cl.get_scene_transition_list().transitions}
        except Exception:  # noqa: BLE001 - an old obs-websocket; not worth failing over
            return
        if cfg.obs.transition not in transitions:
            rep.add("Transition", WARN,
                    f"OBS has no transition called {cfg.obs.transition!r}",
                    'set obs.transition to one OBS has, such as "Fade"')


def _check_sim(rep: Report) -> None:
    """Is iRacing there? A warning when it is not, never a failure: Pylon is
    normally started before the sim, and this check is about telling someone
    whether the blank video they are looking at is expected."""
    if sys.platform != "win32":
        rep.add("iRacing", WARN, "this is not Windows, so the sim cannot be read here",
                "run Pylon on the PC iRacing runs on")
        return
    try:
        import irsdk
    except ImportError:
        rep.add("iRacing", BAD, "the iRacing SDK package is missing",
                "reinstall Pylon")
        return
    ir = irsdk.IRSDK()
    try:
        if ir.startup() and ir.is_initialized and ir.is_connected:
            rep.add("iRacing", OK, "running, telemetry readable")
        else:
            rep.add("iRacing", WARN, "not running, or not in a session",
                    "join a session as a spectator; Pylon picks it up on its own")
    except Exception as e:  # noqa: BLE001
        rep.add("iRacing", WARN, f"could not read the sim ({e})",
                "start iRacing and join a session")
    finally:
        try:
            ir.shutdown()
        except Exception:  # noqa: BLE001, S110 - nothing to do if even this fails
            pass


def _check_ports(rep: Report, settings: ShowSettings) -> None:
    ports = {
        "iRacing connection": settings.bridge_port,
        "timing tower": settings.overlay_http_port,
        "timing data": settings.overlay_ws_port,
        "control panel": settings.control_port,
    }
    taken = [f"{name} ({port})" for name, port in ports.items() if not _port_free(port)]
    if not taken:
        rep.add("Ports", OK, f"{len(ports)} free")
    else:
        # The overwhelmingly likely cause, and the one worth naming first.
        rep.add("Ports", WARN, f"in use: {', '.join(taken)}",
                "another copy of Pylon is probably already running; close it, or "
                "change the ports under [advanced] in Settings")


def run(cfg: Config | None = None, *, settings: ShowSettings = SHOW, connect=None,
        check_sim: bool = True) -> Report:
    """Every check, in dependency order. Never raises."""
    cfg = cfg if cfg is not None else load_config()
    rep = Report()
    _check_config(rep, cfg)
    _check_angles(rep, cfg)
    cl = _check_obs(rep, cfg, settings, connect=connect)
    if cl is not None:
        _check_scenes(rep, cl, cfg)
    if check_sim:
        _check_sim(rep)
    _check_ports(rep, settings)
    return rep
