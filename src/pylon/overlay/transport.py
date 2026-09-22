"""The overlay's transport: an HTTP server for the pages, a WebSocket for the models.

`serve` plays a recording through the world model and pushes tower models to any
connected page; `serve_live` does the same from a bridge's telemetry stream, with the
camera focus taken from what the running director publishes (PublishedShots). Both run
on the one Windows rig now; OBS loads the page as a Browser Source off localhost.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path
from typing import ClassVar

import websockets

from ..camera.bridge import BridgeClient, fetch_session_info
from ..config import load as load_config
from ..director.model import Shot
from ..settings import SHOW
from ..show.shotlink import read_shot
from ..telemetry import PlaybackSource
from ..world import WorldModel
from ..world import run as run_world
from .model import LocalDirector, TowerModel, _shot_from_echo

OVERLAY_DIR = SHOW.overlays_dir


class _OverlayHTTP(http.server.SimpleHTTPRequestHandler):
    """Serve the overlay as UTF-8 so the '·' and arrow glyphs don't mojibake.

    SimpleHTTPRequestHandler otherwise sends text/html with no charset, and
    browsers then fall back to latin-1 (turning "·" into "Â·")."""

    #: `GET /show.json`, the one route here that is not a file on disk.
    #:
    #: The holding cards need to know what the show is called and what colour it
    #: wears, and they have no WebSocket: they are static browser sources that OBS
    #: points at a URL and forgets. The tower gets the same facts in the model it is
    #: already being pushed; this is for the pages that are not being pushed anything.
    #:
    #: Deliberately a REDUCTION of the config (Config.page_view), not a copy of it.
    #: A browser renders these pages, and the file behind them can hold a stream key.
    show: ClassVar[dict] = {}

    # mimetypes reads the REGISTRY on Windows, where .woff2 is normally absent, so the
    # bundled flag font goes out as font/woff2 on Linux and application/octet-stream on
    # the sim rig. Nothing enforces a content type for @font-face, so both work today,
    # which is exactly why it is worth pinning now, before something downstream starts
    # caring (a nosniff policy, a stricter CEF) and the flags quietly become letters
    # again on one platform only.
    _FONT_TYPES: ClassVar[dict[str, str]] = {
        ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    }

    def guess_type(self, path):  # type: ignore[override]
        suffix = Path(path).suffix.lower()
        if suffix in self._FONT_TYPES:
            return self._FONT_TYPES[suffix]
        base = super().guess_type(path)
        if base in ("text/html", "text/plain", "text/css", "text/javascript",
                    "application/javascript"):
            return base + "; charset=utf-8"
        return base

    def do_GET(self):  # type: ignore[override]
        """The one dynamic route, then the static tree."""
        if self.path.split("?")[0] == "/show.json":
            body = json.dumps(self.show).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):  # type: ignore[override]
        """Forbid caching, because OBS's browser is otherwise impossible to update.

        Measured 2026-07-30: an HTML change was live on this server (curl confirmed the
        new markup) and the OBS Browser Source kept rendering the OLD page. The documented
        about:blank-and-back dance did not help: the refetch is real, but the browser
        answers it out of its own cache. Only a cache-busting query string worked, and
        that is not a fix: it has to be invented afresh for every edit, and the moment the
        URL goes back to normal the stale page returns (verified: the graphic vanished
        again).

        SimpleHTTPRequestHandler sends Last-Modified and no Cache-Control, which is an
        open invitation to heuristic caching. These files are always served from disk to
        exactly one local browser, so caching buys nothing and costs an afternoon.
        """
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def _start_http(host: str, port: int, show: dict | None = None) -> None:
    _OverlayHTTP.show = show or {}
    handler = functools.partial(_OverlayHTTP, directory=str(OVERLAY_DIR))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server((host, port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


class PublishedShots:
    """The live overlay's camera focus: what the running director has PUBLISHED.

    In order: the director's hand-off file (show/shotlink), then the shot echoed on the
    bridge's frame stream, then the last real shot we had, because that is where the
    camera still is. NEVER a local director. One used to stand in whenever the file went
    stale, and the moment the director died the name bug went off following a simulated
    shot while the sim's camera sat frozen on the real one, which is the exact bug the
    shot file was built to kill, back for the one case it mattered most. If the director
    goes quiet the last real shot IS the truth about the picture, so it is kept.
    """

    def __init__(self, bridge_client) -> None:
        self.client = bridge_client
        self.last: Shot | None = None

    def read(self) -> tuple[Shot | None, dict | None]:
        """(the shot to focus on, the raw published record it came from). The record
        also carries the instant-replay banner, which rides alongside the model."""
        echo = read_shot() or self.client.latest_shot
        shot = _shot_from_echo(echo) or self.last
        self.last = shot
        return shot, echo

    def current(self, snap=None) -> Shot | None:
        return self.read()[0]


async def _pump_models(bridge_client, on_model, *, refresh_url: str | None = None,
                       refresh_every: float = 60.0) -> None:
    """Turn streamed telemetry frames into overlay models (testable core).

    Runs the world model over frames arriving async from a bridge and calls the
    async `on_model(model)` for each tick. The world model is the same synchronous
    per-frame code as everywhere else; only the frame source is async.

    With refresh_url set, a side task re-fetches session info every refresh_every
    seconds (a throwaway second bridge connection) and hot-swaps it into the world
    model and tower. Team sessions need this: DriverInfo's UserName is whoever is
    in the seat NOW, so every driver swap renames a car and a connect-time
    snapshot of the names drifts wrong over a long race.
    """
    info = bridge_client.info
    wm = WorldModel(info)
    # Focus comes from what the live director publishes (PublishedShots), so the pop-in
    # matches the actual camera; no local director, ever, and that class says why.
    #
    # force_replay=None: the badge asks "is this race happening right now, or are we
    # re-broadcasting something that already happened", and the world model answers it
    # per frame (world.session.TapeBadge). This used to be a hard False, because
    # the sim's IsReplayPlaying alone is 1 for a spectator parked at the live edge and
    # badged a fully live broadcast REPLAY (Spa, 2026-07-26). Pinning it fixed that
    # case by breaking the other one: playing a saved .rpy over the bridge is exactly
    # what this path is used for, and it badged LIVE the whole way through
    # (Nordschleife, 2026-07-30, #62). Whether the TAPE IS STILL BEING WRITTEN tells
    # the two apart without a constant either way. Note that this answers "is this
    # race happening right now", which is a question about the FEED; tape the director
    # rolls mid-race is a question about the SHOT, and #18 owns that one.
    tower = TowerModel(info, force_replay=None)
    shots = PublishedShots(bridge_client)

    async def refresh_loop() -> None:
        while True:
            await asyncio.sleep(refresh_every)
            try:
                fresh = await fetch_session_info(refresh_url)
            except (TimeoutError, OSError, websockets.WebSocketException):
                continue  # bridge hiccup; names refresh next round
            wm.refresh_info(fresh)
            tower.refresh_info(fresh)

    refresher = asyncio.create_task(refresh_loop()) if refresh_url else None
    model = None
    away = False
    try:
        async for frame in bridge_client.frames():
            shot, echo = shots.read()
            banner = (echo or {}).get("replay")
            if banner and banner.get("active") and model is not None:
                # The director's tape is elsewhere: these frames describe the replayed
                # moment. The same isolation as the director's own model (#18) and the
                # director's: not fed. The page keeps the last live board, receded under
                # the letterbox, rather than a board that re-counts the excursion (a
                # pass toasted twice, a fastest lap set in the past) and derives every
                # speed across the seek home.
                away = True
                model = {**model, "instantReplay": banner}
                await on_model(model)
                continue
            if away:
                away = False
                wm.resync()         # the frame back is not continuous with the last seen
            snap = wm.update(frame)
            model = tower.build(snap, shot=shot)
            # The instant-replay banner rides alongside the model rather than inside it:
            # it describes what the DIRECTOR is doing, not what the world looks like, and
            # the world model has no idea an excursion is happening. Distinct from
            # session.replay, which is the sim's IsReplayPlaying and stays true for every
            # frame of a saved tape (#62), so it can never mark an instant replay.
            model["instantReplay"] = banner
            await on_model(model)
    finally:
        if refresher is not None:
            refresher.cancel()


async def serve_live(
    bridge_url: str,
    host: str = SHOW.bind_host,
    ws_port: int = SHOW.overlay_ws_port,
    http_port: int = SHOW.overlay_http_port,
    reconnect: float = SHOW.reconnect,
) -> None:
    """Serve the overlay fed by LIVE telemetry from a bridge (director topology).

    Same browser-facing surface as serve(), but instead of replaying a recording it
    consumes the sim box's telemetry stream off the LAN bridge and pushes the tower
    model to connected overlays. Reconnects if the bridge is not up yet or drops.
    """
    clients: set = set()

    async def handler(ws):
        clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    async def broadcast_model(model: dict) -> None:
        if not clients:
            return
        # Non-blocking fan-out, for the same reason as the bridge's: a preview tab
        # that suspends, or a CEF dock that hangs, must not hold the model for the
        # source that is on air. A dead reader is cut by the keepalive below.
        websockets.broadcast(clients, json.dumps(model))

    # The show's identity, read once here for the pages that have no WebSocket to be
    # pushed it over. Never raises: a missing config is a working default (config.py).
    _start_http(host, http_port, load_config().page_view())
    shown = "localhost" if host in ("0.0.0.0", "") else host
    async with websockets.serve(handler, host, ws_port, ping_interval=10, ping_timeout=10):
        print("live overlay ready:")
        print(f"  http://{shown}:{http_port}/broadcast-overlay.html?ws=ws://{shown}:{ws_port}&live")
        print(f"  (feeding from bridge {bridge_url}; OBS on the sim box uses this box's LAN IP)")
        while True:
            client = BridgeClient(bridge_url)
            try:
                await client.connect()
                await _pump_models(client, broadcast_model, refresh_url=bridge_url)
            except (TimeoutError, OSError, websockets.WebSocketException):
                pass  # bridge not up yet or dropped; retry below
            except (ValueError, KeyError) as e:
                # A malformed handshake or frame. Worth a line; not worth the process,
                # which is meant to be the loop that never dies.
                print(f"overlay: bad message from the bridge ({type(e).__name__}: {e}); "
                      f"reconnecting", flush=True)
            finally:
                await client.aclose()
            await asyncio.sleep(reconnect)


async def serve(
    path: str,
    host: str = SHOW.bind_host,
    ws_port: int = SHOW.overlay_ws_port,
    http_port: int = SHOW.overlay_http_port,
    rate: float = SHOW.overlay_rate,
    loop: bool = True,
) -> None:
    clients: set = set()
    info = PlaybackSource(path).session_info()

    async def handler(ws):
        clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    # The show's identity, read once here for the pages that have no WebSocket to be
    # pushed it over. Never raises: a missing config is a working default (config.py).
    _start_http(host, http_port, load_config().page_view())
    shown = "localhost" if host in ("0.0.0.0", "") else host
    async with websockets.serve(handler, host, ws_port, ping_interval=10, ping_timeout=10):
        print("overlay ready:")
        print(f"  http://{shown}:{http_port}/broadcast-overlay.html?ws=ws://{shown}:{ws_port}")
        print("  (OBS on the sim box: swap localhost for this box's LAN IP, add &live)")
        delay = 1.0 / rate
        while True:
            # fresh director per pass so a re-loop resets cleanly; playback is a replay,
            # never "live", so force the REPLAY badge regardless of the captured flag.
            tower = TowerModel(info, shots=LocalDirector(), force_replay=True)
            for snap in run_world(PlaybackSource(path)):
                if clients:
                    msg = json.dumps(tower.build(snap))
                    for ws in list(clients):
                        try:
                            await ws.send(msg)
                        except websockets.ConnectionClosed:
                            clients.discard(ws)
                await asyncio.sleep(delay)
            if not loop:
                break
        await asyncio.Future()  # keep serving after a single pass
