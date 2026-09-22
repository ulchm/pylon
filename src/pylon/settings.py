"""Every port, host, URL and path the show is wired with, typed once.

The show is three processes and OBS, and they find each other by number: the bridge on
8779, the tower on 8778 (its WebSocket on 8777), the control panel on 8782,
obs-websocket on 4455. Those numbers were once literals in eight modules, and the URLs
OBS loads were retyped beside the module that already defined them. Nothing was wrong,
and nothing could stay right: a port changed in one place would have had the studio
wait out a ready timeout on a worker that was serving somewhere else, with the reason
in a log nobody opens.

So: one frozen dataclass, one default instance (`SHOW`), and everyone reads from it.
The CLI's argparse defaults come from here, the studio builds each worker's command
line from here, and the servers' constructor defaults come from here, so the port the
studio probes is the port the worker binds by construction rather than by agreement.
tests/test_cli.py holds the test that ties the two ends together: every command line
the studio emits parses under the CLI it spawns.

`SHOW` is built from the operator's `config.toml` at import, so a port changed there
is changed everywhere at once. A config that cannot be read leaves the defaults
standing, because a broken file must not stop a broadcast (see config.py).

Tuning knobs (shot lengths, replay timing, the chequer hold) are deliberately NOT here.
Each belongs to the dataclass that consumes it (DirectorConfig, ReplayConfig,
SceneConfig), and the CLI reads its defaults from those classes for the same reason.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import Config, app_dir
from .config import load as load_config

PACKAGE_DIR = Path(__file__).resolve().parent
CHECKOUT_DIR = PACKAGE_DIR.parents[1]   # src/pylon -> the repo root


def bundle_dir() -> Path | None:
    """The unpacked bundle when running as a frozen executable, else None.

    PyInstaller sets `sys.frozen` and unpacks data files next to the executable
    (onedir) or into a temporary tree it points `sys._MEIPASS` at (onefile). Every
    path below has to ask, because in a build there is no `src/pylon` and no
    checkout root above it: getting this wrong ships an installer whose overlay
    serves 404s, which looks exactly like OBS failing to load a browser source.
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent)


def _overlays_dir() -> Path:
    """The browser pages OBS loads: the tower, the cards, the control panel.

    Three homes, checked in the order that makes each kind of install work: inside
    the package (a wheel that ships them), beside the frozen executable (the
    installer), then the checkout (a git clone plus `uv sync`, which is how this is
    developed).
    """
    local = PACKAGE_DIR / "overlays"
    if local.is_dir():
        return local
    bundle = bundle_dir()
    if bundle is not None and (bundle / "overlays").is_dir():
        return bundle / "overlays"
    return CHECKOUT_DIR / "overlays"


def _log_dir() -> Path:
    """Where worker logs go: beside the config, never inside the installation."""
    return app_dir() / "logs"


def _handoff_file(env: str, name: str) -> Path:
    """The small file the director and the overlay pass the current shot through
    (see show/shotlink.py).

    It lives in the temp directory, not the state directory, on purpose: it is
    rewritten several times a second and means nothing once the show is over. The
    environment override exists so two processes can be pointed at the same file when
    the default is not shared, and both sides must set it.
    """
    return Path(os.environ.get(env, str(Path(tempfile.gettempdir()) / name)))


@dataclass(frozen=True)
class ShowSettings:
    # --- ports: one process each. The studio probes these, the README lists them. ---
    bridge_port: int = 8779           # telemetry out, camera and replay commands in
    overlay_ws_port: int = 8777       # tower model pushed to the browser
    overlay_http_port: int = 8778     # the pages themselves
    control_port: int = 8782          # the control panel, docked inside OBS
    obs_port: int = 4455              # obs-websocket

    # --- hosts ---
    bind_host: str = "127.0.0.1"      # what the workers listen on. Loopback by default:
                                      # nothing here needs the LAN, and binding wide is
                                      # what raises a Windows Firewall prompt on a first
                                      # run, which reads as "this program is suspicious"
    loopback: str = "127.0.0.1"       # how the studio and the panel reach them
    page_host: str = "localhost"      # the host inside the URLs OBS's browser sources load
    obs_host: str = "localhost"       # obs-websocket, always on this box

    # --- rates and timing shared by more than one command ---
    live_hz: float = 10.0             # how often the bridge samples the sim's shared memory
    overlay_rate: float = 15.0        # tower ticks per second when replaying a recording
    reconnect: float = 2.0            # seconds between redials of a bridge that dropped

    # --- OBS ---
    canvas: tuple[int, int] = (1920, 1080)
    fps: int = 60
    scene_transition: str = "Fade"    # by NAME in OBS. Fade is in every install; a
                                      # stinger has to be created in OBS by hand first,
                                      # because obs-websocket cannot create transitions

    # --- paths ---
    overlays_dir: Path = field(default_factory=_overlays_dir)
    log_dir: Path = field(default_factory=_log_dir)
    shot_file: Path = field(default_factory=lambda: _handoff_file(
        "PYLON_SHOT_FILE", "pylon_current_shot.json"))

    # --- URLs, built from the above so they cannot disagree with it ---
    def bridge_url(self, host: str | None = None, port: int | None = None) -> str:
        """The bridge WebSocket, as the overlay and the director dial it."""
        return f"ws://{host or self.loopback}:{port or self.bridge_port}"

    def control_url(self) -> str:
        return f"http://{self.loopback}:{self.control_port}/"

    def overlay_page_url(self, host: str | None = None) -> str:
        """The timing tower as OBS's browser source loads it (`&live`: a real feed, not a
        recording, so the page shows the LIVE badge and hides the playback controls)."""
        h = host or self.page_host
        return (f"http://{h}:{self.overlay_http_port}/broadcast-overlay.html"
                f"?ws=ws://{h}:{self.overlay_ws_port}&live")

    def cards_url(self, host: str | None = None) -> str:
        """The holding cards; obs-setup appends `?card=` per scene."""
        return f"http://{host or self.page_host}:{self.overlay_http_port}/cards.html"

    @classmethod
    def from_config(cls, cfg: Config) -> ShowSettings:
        """The numbers this operator's config asks for, over the defaults."""
        return cls(
            bridge_port=cfg.advanced.bridge_port,
            overlay_http_port=cfg.advanced.overlay_http_port,
            overlay_ws_port=cfg.advanced.overlay_ws_port,
            control_port=cfg.advanced.control_port,
            obs_host=cfg.obs.host,
            obs_port=cfg.obs.port,
            scene_transition=cfg.obs.transition,
        )


def _show_from_config() -> ShowSettings:
    """`SHOW` at import. Every failure answers with the defaults, deliberately: this
    runs before anything can report an error, and a show that starts on default ports
    is recoverable in a way that an exception during `import pylon.settings` is not.
    """
    try:
        return ShowSettings.from_config(load_config())
    except Exception:  # noqa: BLE001 - never let a config file stop the package importing
        return ShowSettings()


SHOW = _show_from_config()


def reload_show() -> ShowSettings:
    """Re-read the config into `SHOW`. Called by the studio when the panel saves.

    Rebinds the module global rather than mutating the frozen instance, so anything
    holding the old one keeps a consistent view of the numbers it started with.
    """
    global SHOW
    SHOW = _show_from_config()
    return SHOW


@dataclass(frozen=True)
class StreamSettings:
    """Where OBS sends the programme.

    From the config file rather than the environment, and optional: unset means
    `pylon obs-setup` leaves whatever destination OBS already has, which is what
    someone who already streams to their own channel wants.

    The split follows OBS's own "Custom" service: `server` is the RTMP application
    URL, `key` is everything after it. For Twitch that is `rtmp://live.twitch.tv/app`
    and the key from the Creator Dashboard.
    """

    server: str = ""
    key: str = ""

    def __bool__(self) -> bool:
        return bool(self.server)

    @classmethod
    def from_config(cls, cfg: Config) -> StreamSettings:
        return cls(server=cfg.obs.stream_server.strip(), key=cfg.obs.stream_key.strip())


def stream_settings(cfg: Config | None = None) -> StreamSettings:
    return StreamSettings.from_config(cfg if cfg is not None else load_config())


def with_ports(settings: ShowSettings, **ports) -> ShowSettings:
    """A copy with some ports changed, for a test or a second show on one box."""
    return replace(settings, **ports)
