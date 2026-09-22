"""The one file an operator edits: `config.toml`.

The broadcaster this grew out of was configured by fourteen environment variables,
which is a reasonable interface for the person who wrote it and no interface at all
for someone who has never opened a terminal. Everything a show differs by (its
name, its colour, where OBS is, whether replays roll) now lives in one commented
TOML file in the user's own data directory, written on first run with every value
present and explained. Nothing here reads the environment except to find that
directory.

Three rules this file keeps:

**A missing or broken config is not an error.** `load()` always returns a Config.
A file that fails to parse is reported through `Config.problems` and the defaults
stand, because a typo in a colour must not stop a broadcast that is about to start.

**Unknown keys survive a rewrite.** The dock's Settings tab saves this file, and a
key it does not know about (a hand-added one, or one from a newer version) is kept
rather than silently dropped.

**Defaults are a working show.** Pylon with no config at all directs a race: the
only thing it cannot guess is the OBS password, and it reads that from OBS's own
config when it can (obs/discover.py).
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import get_type_hints

APP_NAME = "Pylon"
CONFIG_NAME = "config.toml"


def app_dir() -> Path:
    """Where the config, the logs and any saved recordings live.

    Per-user and outside the installation, so an upgrade that replaces Program Files
    cannot take the operator's settings with it, and so nothing needs administrator
    rights to write.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME.lower()


def config_path() -> Path:
    """The config file itself. `PYLON_CONFIG` overrides it, which is how the tests
    run against a temporary one and how a second show on one box keeps its own."""
    override = os.environ.get("PYLON_CONFIG", "").strip()
    return Path(override) if override else app_dir() / CONFIG_NAME


# --- the schema ---------------------------------------------------------------
#
# One dataclass per TOML table. Types are load-bearing: `_coerce` uses the declared
# type of each field to decide what a value from the file has to become, so a port
# typed as a string in the file still arrives as an int and a "yes" still arrives as
# a bool. Anything that cannot be coerced is reported and the default stands.
#
# The types are read with `get_type_hints`, never `field.type`. This module uses
# `from __future__ import annotations`, which makes every annotation a STRING, so
# `field.type is bool` is False for a field declared `bool` and every value would
# pass through uncoerced. That failure is silent and arrives as a port that is a
# string and a checkbox that is the text "off", which is truthy.


@dataclass
class ShowConfig:
    """What this broadcast is called. Drawn on the holding cards and the channel bug."""

    #: "Thursday Night Racing". Empty is fine: the cards then carry the mark alone.
    name: str = ""
    #: A short form for tight furniture ("TNR"). Empty: `name` is used everywhere.
    tag: str = ""
    #: Free text shown as the round chip: "Round 4", "Feature Race", "Heat 2".
    #: This is the field the league feed used to supply; now it is simply typed.
    round: str = ""
    #: The line under the title on the "starting soon" card.
    subtitle: str = ""


@dataclass
class LookConfig:
    """The paint. One colour and one logo carry the whole identity."""

    #: Any CSS colour. Everything else in the overlay is derived from it.
    colour: str = "#38BDF8"
    #: A logo for the channel bug and the cards: a file in this directory or a URL.
    #: Empty means the show's name is set as type instead, which needs no art at all.
    logo: str = ""
    #: Show the timing tower at all. Off is for operators who have their own overlay.
    tower: bool = True
    #: Draw the channel bug in the corner.
    bug: bool = True


@dataclass
class ObsConfig:
    """How to reach OBS, and how much of it Pylon is allowed to drive."""

    host: str = "localhost"
    port: int = 4455
    #: obs-websocket's password. Empty means "find it yourself", which works whenever
    #: OBS is on this machine: see obs/discover.py. A wrong password is reported at
    #: startup rather than at the first scene change.
    password: str = ""
    #: Let Pylon switch between the programme scene and the holding cards.
    #: Off: it directs the in-sim cameras and leaves the switcher alone.
    scenes: bool = True
    #: The transition between them, by its name in OBS. "Fade" exists in every
    #: install; a stinger has to be added in OBS by hand before it can be named here.
    transition: str = "Fade"
    #: Where OBS sends the programme, if Pylon should set that up too. Both empty
    #: (the default) means OBS keeps whatever destination it already has, which is
    #: what someone who already streams wants.
    stream_server: str = ""
    stream_key: str = ""


@dataclass
class DirectorConfigFile:
    """How the race is covered."""

    #: Roll an instant replay after a crash. Costs nothing when nothing happens.
    replays: bool = True
    #: The camera personality: "tv" (trackside broadcast cameras, the default),
    #: "onboard" (in-car and nose, the only one that uses the cockpit view) or
    #: "wide" (weaves in the helicopter and the blimp for drama). Anything else
    #: falls back to "tv" with a line saying so.
    angles: str = "tv"
    #: Car numbers to keep an eye on: "64", "17". A car on this list is worth more
    #: to the director than its position alone says, so a friend mid-pack still gets
    #: screen time. Empty is a neutral show that follows the racing.
    favourites: list[str] = field(default_factory=list)
    #: Seconds a shot must hold before the director may cut away. Lower is busier.
    min_shot: float = 6.0


@dataclass
class AdvancedConfig:
    """Ports. Nobody should need these; they exist for a box already using one."""

    bridge_port: int = 8779
    overlay_http_port: int = 8778
    overlay_ws_port: int = 8777
    control_port: int = 8782


@dataclass
class Config:
    show: ShowConfig = field(default_factory=ShowConfig)
    look: LookConfig = field(default_factory=LookConfig)
    obs: ObsConfig = field(default_factory=ObsConfig)
    director: DirectorConfigFile = field(default_factory=DirectorConfigFile)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)

    #: Human-readable complaints about the file that was read: a bad type, an
    #: unparseable document, a key nobody knows. Never raised, always reportable:
    #: the studio prints them at startup and the dock shows them in the Settings tab.
    problems: list[str] = field(default_factory=list, compare=False)
    #: Keys the file carried that this version has no field for. Preserved so that
    #: saving from an older build cannot delete a newer build's settings.
    extra: dict = field(default_factory=dict, compare=False)

    def page_view(self) -> dict:
        """What a browser source is allowed to know: the show's identity and paint.

        Served as `/show.json` to the cards and the tower, which have no WebSocket.
        Deliberately a reduction: no ports, no password, nothing that is not
        already visible on screen, because these pages are rendered by a browser
        and the file behind them holds a stream key.
        """
        return {
            "name": self.show.name,
            "tag": self.show.tag or self.show.name,
            "round": self.show.round,
            "subtitle": self.show.subtitle,
            "colour": self.look.colour,
            "logo": self.look.logo,
            "tower": self.look.tower,
            "bug": self.look.bug,
        }


# --- reading ------------------------------------------------------------------

def _hints(obj) -> dict:
    """Resolved field types for a config table. See the note above about strings."""
    return get_type_hints(type(obj))


def _coerce(value, want, where: str, problems: list[str]):
    """Turn a TOML value into the type the field declares, or complain and give up.

    TOML has real types, so this is mostly a pass-through. It earns its place on the
    two the operator gets wrong by hand: a quoted port ("8779") and a spelled-out
    boolean (yes / on / true), both of which are what someone writes when they are
    editing a config file rather than programming.
    """
    if want is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {
                "true", "yes", "on", "1", "false", "no", "off", "0"}:
            return value.strip().lower() in {"true", "yes", "on", "1"}
        problems.append(f"{where}: expected true or false, got {value!r}; using the default")
        return None
    if want is int:
        if isinstance(value, bool):  # bool is an int; not what anyone means here
            problems.append(f"{where}: expected a number, got {value!r}; using the default")
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            problems.append(f"{where}: expected a number, got {value!r}; using the default")
            return None
    if want is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            problems.append(f"{where}: expected a number, got {value!r}; using the default")
            return None
    if want is str:
        if isinstance(value, str):
            return value
        problems.append(f"{where}: expected text, got {value!r}; using the default")
        return None
    if want == list[str]:
        if isinstance(value, str):      # "64, 17" is what a person types in a text box
            return [p.strip() for p in value.split(",") if p.strip()]
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        problems.append(f"{where}: expected a list, got {value!r}; using the default")
        return None
    return value


def from_dict(doc: dict) -> Config:
    """Build a Config from a parsed document, keeping what it does not understand."""
    cfg = Config()
    extra: dict = {}
    for section in fields(Config):
        if not is_dataclass(section.type) and section.name in ("problems", "extra"):
            continue
        table = doc.get(section.name)
        if table is None:
            continue
        if not isinstance(table, dict):
            cfg.problems.append(f"[{section.name}] should be a section; ignoring it")
            continue
        target = getattr(cfg, section.name)
        hints = _hints(target)
        known = {f.name: hints.get(f.name, str) for f in fields(target)}
        for key, value in table.items():
            if key not in known:
                extra.setdefault(section.name, {})[key] = value
                continue
            coerced = _coerce(value, known[key], f"{section.name}.{key}", cfg.problems)
            if coerced is not None:
                setattr(target, key, coerced)
    # Sections nobody knows about, kept whole.
    for key, value in doc.items():
        if key not in {f.name for f in fields(Config)}:
            extra[key] = value
    cfg.extra = extra
    return cfg


def load(path: Path | None = None) -> Config:
    """Read the config, or hand back the defaults. Never raises, never blocks.

    A file that does not exist is not a problem worth reporting: that is a first run,
    and `ensure_config` is what writes one. A file that exists and does not parse IS
    worth reporting, loudly, because the operator changed something and the show is
    about to behave in a way they did not intend.
    """
    path = path or config_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return Config()
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        cfg = Config()
        cfg.problems.append(f"{path} could not be read ({e}); running on the defaults")
        return cfg
    return from_dict(doc)


# --- writing ------------------------------------------------------------------
#
# Hand-rolled, because the alternative is a dependency for four value types and
# because a config a person is expected to edit has to keep its comments. A
# round-trip-preserving TOML library would keep them; a plain writer would not,
# and this writer sidesteps the question by regenerating the comments from the
# template every time. The template and the dataclasses are checked against each
# other in tests/test_config.py, so a field added without a comment fails there.

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


#: The comment above each key in the written file. Every field needs one; the test
#: suite fails if a field is added here without it, because a config file's comments
#: ARE its documentation for the person this product is for.
COMMENTS: dict[str, str] = {
    "show": "What this broadcast is called. Shown on the holding cards and the corner bug.",
    "show.name": "The name of your show or league. Leave empty for no name on screen.",
    "show.tag": "A short form for tight spaces. Empty: the full name is used.",
    "show.round": 'Free text for the round chip: "Round 4", "Feature Race". Empty: no chip.',
    "show.subtitle": 'The line under the title on the "starting soon" card.',
    "look": "The paint. One colour and one logo carry the whole look.",
    "look.colour": "Any CSS colour. The rest of the overlay is derived from it.",
    "look.logo": "A logo file in this folder, or a URL. Empty: the show's name is set as type.",
    "look.tower": "Show the timing tower. Off if you have an overlay of your own.",
    "look.bug": "Draw the small logo in the corner.",
    "obs": "How to reach OBS. Enable its WebSocket server first: Tools > WebSocket Server Settings.",
    "obs.host": "Almost always localhost: OBS on this machine.",
    "obs.port": "The port in that same OBS dialog. 4455 unless you changed it.",
    "obs.password": "Empty means Pylon reads it from OBS's own settings, which works on this machine.",
    "obs.scenes": "Let Pylon switch between the race and the holding cards.",
    "obs.transition": 'The transition between them, by name in OBS. "Fade" exists everywhere.',
    "obs.stream_server": "Where OBS streams to. Both empty: OBS keeps the destination it has.",
    "obs.stream_key": "The stream key for the server above. Treat this file as a secret if you set it.",
    "director": "How the race gets covered.",
    "director.replays": "Roll an instant replay after a crash.",
    "director.angles": 'Camera style: "tv" (trackside), "onboard" (in-car) or "wide".',
    "director.favourites": 'Car numbers to favour, e.g. ["64", "17"]. Empty: follow the racing.',
    "director.min_shot": "Seconds a shot holds before the director may cut. Lower is busier.",
    "advanced": "Ports. Change these only if something else on this PC already uses one.",
    "advanced.bridge_port": "Telemetry out, camera commands in.",
    "advanced.overlay_http_port": "Serves the overlay pages to OBS.",
    "advanced.overlay_ws_port": "Pushes the timing data to them.",
    "advanced.control_port": "The control panel you open inside OBS.",
}

HEADER = """\
# Pylon: automatic iRacing broadcast direction.
#
# Everything Pylon can be told is in this file, and every line has a default that
# works, so a setting you do not care about can be left exactly as it is.
#
# Easier than editing this: the Settings tab of the control panel, which is the same
# settings as a form. Either way, a change reaches the overlay when the timing tower
# restarts (there is a button for that on its row in the panel).
#
# This file is yours: Pylon rewrites it when you save from the control panel, but
# it keeps anything it does not recognise, so notes of your own are safe here.
"""


def to_toml(cfg: Config) -> str:
    """The config as a commented file, ready to write.

    Written whole rather than patched: every key present, every comment regenerated,
    so a file from an older version gains the new options with their explanations the
    first time it is saved instead of quietly lacking them.
    """
    out = [HEADER]
    for section in fields(Config):
        if section.name in ("problems", "extra"):
            continue
        table = getattr(cfg, section.name)
        out.append("")
        if COMMENTS.get(section.name):
            out.append(f"# {COMMENTS[section.name]}")
        out.append(f"[{section.name}]")
        for f in fields(table):
            note = COMMENTS.get(f"{section.name}.{f.name}")
            if note:
                out.append(f"# {note}")
            out.append(f"{f.name} = {_toml_value(getattr(table, f.name))}")
            if note:
                out.append("")
        extra_keys = cfg.extra.get(section.name, {})
        if extra_keys:
            out.append("# Kept from your file; this version of Pylon does not use these.")
            for key, value in extra_keys.items():
                out.append(f"{key} = {_toml_value(value)}")
        while out and out[-1] == "":
            out.pop()
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def save(cfg: Config, path: Path | None = None) -> Path:
    """Write the config atomically and return where it went.

    Atomic because the control panel writes this while the show is running and the
    workers read it: a half-written file read by a restarting worker is a broadcast
    that comes back up with default paint in the middle of a race.
    """
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(to_toml(cfg), encoding="utf-8")
    os.replace(tmp, path)
    return path


def ensure_config(path: Path | None = None) -> tuple[Config, Path, bool]:
    """Read the config, writing a fully commented default one if there is none.

    Returns (config, path, created). `created` is what the first-run flow keys off:
    it is the difference between "welcome, here is what to do next" and a silent
    start, and it must be true exactly once per machine.
    """
    path = path or config_path()
    if path.exists():
        return load(path), path, False
    cfg = Config()
    try:
        save(cfg, path)
    except OSError as e:
        cfg.problems.append(f"could not write {path} ({e}); running on the defaults")
        return cfg, path, False
    return cfg, path, True


def as_dict(cfg: Config) -> dict:
    """The config as plain data, for the control panel's Settings tab."""
    doc = {f.name: asdict(getattr(cfg, f.name))
           for f in fields(Config) if f.name not in ("problems", "extra")}
    return doc
