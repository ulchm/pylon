#!/usr/bin/env python3
"""Capture the README's screenshots from the REAL pages.

    uv run tools/screenshots.py [--chrome /path/to/chrome]

Writes PNGs into docs/images/. Run it when the graphics change and commit the
result; this is not a build step, it is how the pictures get made.

Everything here is captured from the actual overlay served by the actual HTTP
handler, with a tower model built by the actual pipeline (synthetic source ->
world model -> TowerModel). Nothing is mocked up in a drawing program, so a
screenshot that looks wrong means the page IS wrong, which is the only kind of
screenshot worth putting in front of somebody deciding whether to install this.

The control panel is the one page that needs help: it polls a studio that is not
running, so a stub answers its two endpoints with a plausible show.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pylon.config import Config, LookConfig, ShowConfig
from pylon.director.model import Shot, ShotKind
from pylon.overlay.model import TowerModel
from pylon.overlay.transport import _OverlayHTTP
from pylon.settings import SHOW
from pylon.telemetry import SyntheticSource
from pylon.world import WorldModel

OUT = ROOT / "docs" / "images"

#: The show the screenshots are OF. A named, coloured show rather than the bare
#: defaults, because that is what the pictures are meant to demonstrate.
DEMO = Config(show=ShowConfig(name="Thursday Night Racing", tag="TNR",
                              round="Round 7", subtitle="Watkins Glen"),
              look=LookConfig(colour="#38BDF8"))


# --- a browser ------------------------------------------------------------------

def find_chrome(explicit: str | None) -> str | None:
    for name in (explicit, "google-chrome-stable", "google-chrome", "chromium",
                 "chromium-browser"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


class Page:
    """One CDP connection: evaluate JavaScript, capture a PNG."""

    def __init__(self, ws_url: str):
        import websockets

        self.loop = asyncio.new_event_loop()
        self._connect = websockets.connect
        self.ws = self.loop.run_until_complete(
            websockets.connect(ws_url, max_size=64 * 1024 * 1024))
        self._id = 0

    def _call(self, method: str, **params):
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method, "params": params})

        async def go():
            await self.ws.send(msg)
            while True:
                got = json.loads(await self.ws.recv())
                if got.get("id") == self._id:
                    return got

        return self.loop.run_until_complete(asyncio.wait_for(go(), 30.0))

    def eval(self, expression: str):
        r = self._call("Runtime.evaluate", expression=expression,
                       returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r.get("result", {}):
            raise AssertionError(f"page threw: {r['result']['exceptionDetails']}")
        return r["result"]["result"].get("value")

    def wait_for(self, expression: str, timeout: float = 15.0):
        deadline = time.time() + timeout
        while True:
            if self.eval(expression):
                return
            if time.time() > deadline:
                raise AssertionError(f"never true: {expression}")
            time.sleep(0.05)

    def shot(self, path: Path, *, width: int, height: int) -> None:
        self._call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                   deviceScaleFactor=1, mobile=False)
        time.sleep(0.45)                       # let transitions and fonts settle
        r = self._call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        import base64
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(r["result"]["data"]))
        print(f"  wrote {path.relative_to(ROOT)}")

    def close(self):
        try:
            self.loop.run_until_complete(self.ws.close())
        finally:
            self.loop.close()


def launch(exe: str, url: str, width: int, height: int):
    """Headless Chrome on one page. Returns (proc, Page, profile_dir)."""
    profile = tempfile.mkdtemp(prefix="pylon-shot-")
    args = [exe, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--mute-audio",
            "--hide-scrollbars", "--force-device-scale-factor=1",
            "--remote-debugging-port=0", f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "--autoplay-policy=no-user-gesture-required", url]
    if hasattr(sys, "getwindowsversion") is False and hasattr(__import__("os"), "geteuid"):
        import os
        if os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    port = None
    deadline = time.time() + 25
    while time.time() < deadline:
        line = proc.stderr.readline().decode(errors="replace")
        if "DevTools listening on ws://" in line:
            port = line.split("ws://")[1].split("/")[0].split(":")[1]
            break
    if port is None:
        proc.kill()
        raise RuntimeError("Chrome never reported a DevTools port")

    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                targets = json.loads(r.read())
            page = next((t for t in targets if t.get("type") == "page"), None)
            if page:
                return proc, Page(page["webSocketDebuggerUrl"]), profile
        except Exception:
            pass
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError("Chrome never opened a page target")


# --- the pages ------------------------------------------------------------------

def serve_overlays(show: dict):
    _OverlayHTTP.show = show
    handler = functools.partial(_OverlayHTTP, directory=str(SHOW.overlays_dir))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def race(num_cars: int = 18, seed: int = 3, seconds: float = 420.0):
    """A real race through the real pipeline, not a hand-written model.

    Long enough that cars have COMPLETED laps: the tower's last-lap column and its
    fastest-lap treatment are blank until somebody crosses the line, and a
    screenshot of a race in its opening seconds shows none of what this does.
    """
    src = SyntheticSource(num_cars=num_cars, duration_s=seconds, hz=10.0, seed=seed)
    info = src.session_info()
    wm = WorldModel(info)
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)
    return TowerModel(info), snap


def capture_tower(exe: str, base: str) -> None:
    print("tower:")
    proc, page, profile = launch(exe, f"{base}/broadcast-overlay.html?harness", 1920, 1080)
    try:
        page.wait_for("document.readyState === 'complete' && !!window.overlay")
        tower, snap = race()

        # A battle for the lead: two pop-ins and the gap between them, which is the
        # shot that shows most of what the overlay does at once.
        ahead, behind = snap.order[0], snap.order[1]
        shot = Shot(ShotKind.BATTLE, f"battle:{ahead}:{behind}", behind,
                    "P1 vs P2", pair=(ahead, behind))
        page.eval(f"window.overlay.render({json.dumps(tower.build(snap, shot=shot))}); true")
        page.wait_for("document.querySelectorAll('.row').length > 4")
        page.shot(OUT / "tower.png", width=1920, height=1080)
    finally:
        page.close()
        proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def capture_card(exe: str, base: str) -> None:
    print("holding card:")
    url = f"{base}/cards.html?card=soon&track=watkins-glen&in=900"
    proc, page, profile = launch(exe, url, 1920, 1080)
    try:
        page.wait_for("document.querySelector('.card__circuit') !== null")
        page.shot(OUT / "card.png", width=1920, height=1080)
    finally:
        page.close()
        proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


PANEL_STATUS = {
    "stopping": False,
    "streaming": True,
    "setup": {"running": False, "ok": True, "at": 0.0,
              "lines": ["Scene 'TNR - Broadcast' is ready at 1920x1080."]},
    "workers": [
        {"name": "bridge", "pid": 4812, "alive": True, "listening": True,
         "restarts": 0, "failed": False, "ok": True, "starting": False, "error": ""},
        {"name": "overlay", "pid": 4820, "alive": True, "listening": True,
         "restarts": 0, "failed": False, "ok": True, "starting": False, "error": ""},
        {"name": "director", "pid": 4831, "alive": True, "listening": None,
         "restarts": 0, "failed": False, "ok": True, "starting": False, "error": ""},
    ],
}


def serve_panel():
    """The control panel polls a studio. Stub its two endpoints with a live show."""
    from pylon.config import as_dict

    settings = {"path": r"C:\Users\you\AppData\Local\Pylon\config.toml",
                "settings": as_dict(DEMO), "problems": []}
    settings["settings"]["obs"]["password"] = "__kept__"

    class Handler(_OverlayHTTP):
        def do_GET(self):
            route = self.path.split("?")[0]
            body = None
            if route == "/api/status":
                body = json.dumps(PANEL_STATUS).encode()
            elif route == "/api/settings":
                body = json.dumps(settings).encode()
            if body is not None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("", "/"):
                self.path = "/control.html"
            return super().do_GET()

    handler = functools.partial(Handler, directory=str(SHOW.overlays_dir))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def capture_panel(exe: str) -> None:
    print("control panel:")
    srv, url = serve_panel()
    # Roughly the width OBS gives a docked panel.
    proc, page, profile = launch(exe, url, 360, 620)
    try:
        page.wait_for("document.querySelectorAll('.row').length >= 3")
        page.shot(OUT / "panel.png", width=360, height=430)
        page.eval("document.getElementById('tabSettings').click(); true")
        page.wait_for("document.getElementById('show_name').value !== ''")
        page.shot(OUT / "panel-settings.png", width=360, height=900)
    finally:
        page.close()
        proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
        srv.shutdown()


def shrink(path: Path, *, max_width: int = 1280) -> None:
    """Make a screenshot small enough to live in a repository people clone.

    A 1920x1080 PNG of these pages is about two megabytes, and five of them would
    be most of the checkout. They are flat colour over a subtle gradient, so an
    adaptive 256-colour palette at 1280 wide is visually identical at the size a
    README renders them and about a quarter of the weight.
    """
    try:
        from PIL import Image
    except ImportError:
        print(f"  (no Pillow; {path.name} left full size)")
        return
    im = Image.open(path).convert("RGBA")
    if im.width > max_width:
        im = im.resize((max_width, round(im.height * max_width / im.width)),
                       Image.LANCZOS)
    before = path.stat().st_size
    im.convert("RGB").quantize(colors=256).save(path, optimize=True)
    print(f"  {path.name}: {before // 1024}K -> {path.stat().st_size // 1024}K")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chrome", default=None, help="the browser to drive")
    args = ap.parse_args()

    exe = find_chrome(args.chrome)
    if exe is None:
        print("no Chrome or Chromium found; pass --chrome /path/to/chrome")
        return 2
    print(f"using {exe}")

    srv, base = serve_overlays(DEMO.page_view())
    try:
        capture_tower(exe, base)
        capture_card(exe, base)
    finally:
        srv.shutdown()
    capture_panel(exe)

    print("shrinking:")
    for png in sorted(OUT.glob("*.png")):
        shrink(png)
    print(f"\ndone. {OUT.relative_to(ROOT)} holds the images.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
