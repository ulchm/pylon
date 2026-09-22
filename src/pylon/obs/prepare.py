"""The two OBS settings a person would otherwise have to find and change by hand.

`obs-setup` talks to a RUNNING OBS over its WebSocket and builds scenes. This does
the opposite job: it edits OBS's own configuration files while OBS is CLOSED, to
arrange the two things that must be true before a WebSocket connection is possible
at all.

  1. **obs-websocket ships disabled.** Nothing can talk to OBS until someone ticks
     Tools > WebSocket Server Settings > Enable. That was the one instruction in
     the README that could not be automated, and it is exactly the kind of step
     that loses people.
  2. **The control panel has to be added as a Custom Browser Dock**, which is six
     clicks and a URL typed correctly.

**OBS MUST BE CLOSED.** It reads these files at startup and rewrites them on exit,
so an edit made while it is running is reverted the moment it closes, silently.
Everything here refuses to run rather than make a change that will be undone.

Both edits are ADDITIVE and idempotent. A machine that already runs another
broadcaster has a websocket password and a dock of its own, and this must leave
both exactly as they are: the dock list is merged, not replaced, and an already
enabled server is reported and left alone. That is not hypothetical; it is the
author's own sim rig.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .output import obs_dir

#: OBS's own key for the list of Custom Browser Docks, in user.ini's [BasicWindow].
DOCKS_KEY = "ExtraBrowserDocks"


@dataclass
class PrepareReport:
    ok: bool = True
    changed: bool = False
    lines: list[str] = field(default_factory=list)
    #: True when the only thing standing in the way is that OBS is open.
    obs_running: bool = False

    def say(self, line: str) -> None:
        self.lines.append(line)


def obs_is_running() -> bool:
    """Is OBS up? Editing its config underneath it is worse than not editing it.

    Answers False when it cannot tell, on purpose: a false "yes" would refuse to do
    anything on a machine where the check simply does not work, and the edits
    themselves are idempotent and backed up.
    """
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"],
                                 capture_output=True, text=True, timeout=10, check=False)
            return "obs64" in out.stdout.lower()
        out = subprocess.run(["pgrep", "-x", "obs"], capture_output=True,
                             timeout=10, check=False)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --- 1. the websocket server --------------------------------------------------

def websocket_config_path(base: Path | None = None) -> Path:
    return (base or obs_dir()) / "plugin_config/obs-websocket/config.json"


def enable_websocket(base: Path | None = None) -> tuple[bool, str]:
    """Turn obs-websocket on, keeping whatever password OBS already generated.

    Returns (changed, message). A missing file means OBS has never run, or never
    loaded the plugin; one is written with a fresh password rather than telling the
    operator to go and start OBS first, because the point of this is to remove
    steps. OBS reads it at startup and accepts it.
    """
    path = websocket_config_path(base)
    doc: dict = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return False, f"could not read {path} ({e}); leaving OBS alone"
        if not isinstance(doc, dict):
            return False, f"{path} is not what obs-websocket writes; leaving OBS alone"

    if doc.get("server_enabled") is True:
        return False, "obs-websocket was already enabled"

    doc["server_enabled"] = True
    doc.setdefault("server_port", 4455)
    doc.setdefault("auth_required", True)
    # Only ever generated when OBS has not made one. Never regenerated: another
    # broadcaster on this PC is authenticating with it.
    if not doc.get("server_password"):
        doc["server_password"] = secrets.token_urlsafe(16)
    doc.setdefault("alerts_enabled", False)
    doc.setdefault("first_load", False)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        return False, f"could not write {path} ({e})"
    return True, "obs-websocket enabled"


# --- 2. the control panel dock ------------------------------------------------

def user_ini_path(base: Path | None = None) -> Path:
    """OBS 30+ keeps the front-end settings here. `global.ini` is a 0-byte red
    herring on those versions and is deliberately not consulted."""
    return (base or obs_dir()) / "user.ini"


def _read_ini(path: Path) -> tuple[str, str]:
    """(text, newline). Read as text but WRITTEN BACK LINE FOR LINE.

    Not configparser: this file holds a long base64 Qt `DockState` and a pile of
    keys OBS owns, and a round trip through a parser reorders sections, changes
    case and can drop what it does not understand. Corrupting it costs somebody
    their whole OBS layout, so only the one line we care about is ever touched.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return text, ("\r\n" if "\r\n" in text else "\n")


def _docks(text: str) -> tuple[list, str | None]:
    """The dock list OBS has, and the raw line it came from (None if absent)."""
    m = re.search(rf"^{DOCKS_KEY}=(.*)$", text, flags=re.MULTILINE)
    if not m:
        return [], None
    try:
        docks = json.loads(m.group(1))
    except ValueError:
        return [], m.group(0)
    return (docks if isinstance(docks, list) else []), m.group(0)


def add_browser_dock(title: str, url: str, base: Path | None = None) -> tuple[bool, str]:
    """Add one Custom Browser Dock, keeping every dock already there.

    Matching is on the URL, not the title: a person who renamed our dock still has
    our dock, and adding a second pointing at the same page would be the clutter
    this is supposed to save them.
    """
    path = user_ini_path(base)
    if not path.exists():
        return False, (f"no {path}; start OBS once so it writes its settings, "
                       f"then run this again")
    try:
        text, nl = _read_ini(path)
    except OSError as e:
        return False, f"could not read {path} ({e})"

    docks, line = _docks(text)
    if any(isinstance(d, dict) and d.get("url") == url for d in docks):
        return False, f"the {title} dock was already there"

    # OBS keys each dock with a 32-character hex uuid and no dashes.
    docks.append({"title": title, "url": url, "uuid": secrets.token_hex(16)})
    new_line = f"{DOCKS_KEY}={json.dumps(docks)}"

    if line is not None:
        text = text.replace(line, new_line, 1)
    else:
        # No docks yet. The key belongs in [BasicWindow]; without that section
        # OBS ignores it, so say so rather than writing somewhere it does nothing.
        m = re.search(r"^\[BasicWindow\]\s*$", text, flags=re.MULTILINE)
        if not m:
            return False, (f"{path} has no [BasicWindow] section; add the dock by "
                           f"hand in OBS (View > Docks > Custom Browser Docks)")
        text = text[:m.end()] + nl + new_line + text[m.end():]

    try:
        backup = path.with_suffix(".ini.pylon-backup")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        tmp = path.with_suffix(".ini.tmp")
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(path)
    except OSError as e:
        return False, f"could not write {path} ({e})"
    return True, f"added the {title} dock"


# --- both, as one job ---------------------------------------------------------

def prepare_obs(*, panel_url: str, panel_title: str = "Pylon",
                base: Path | None = None, force: bool = False) -> PrepareReport:
    """Arrange the two things that must be true before OBS can be talked to."""
    rep = PrepareReport()
    root = base or obs_dir()

    if not root.exists():
        rep.ok = False
        rep.say(f"OBS does not seem to be installed: nothing at {root}")
        rep.say("Install OBS, start it once, then run this again.")
        return rep

    if obs_is_running() and not force:
        rep.ok = False
        rep.obs_running = True
        rep.say("OBS is running, and it rewrites these settings when it closes,")
        rep.say("so a change made now would be silently undone.")
        rep.say("Close OBS, run this again, then start OBS.")
        return rep

    for changed, msg in (enable_websocket(base=root),
                         add_browser_dock(panel_title, panel_url, base=root)):
        rep.changed |= changed
        rep.say(("  changed: " if changed else "  already: ") + msg)

    if rep.changed:
        rep.say("Start OBS now. The control panel is under View > Docks.")
    else:
        rep.say("Nothing to do: OBS was already set up for Pylon.")
    return rep
