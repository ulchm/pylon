"""The LAN bridge transport: the bidirectional WebSocket, no brain attached.

The bridge is the design's "one component that owns both directions" (DESIGN.md
sections 2, 12), realized over a single WebSocket:

  server -> client : telemetry frames (a session message, then one per frame)
  client -> server : camera commands (CamCommand)

- BridgeServer runs on the sim box (inside the GUI agent). Given a telemetry
  `source` it streams frames to each client; it always accepts camera commands and
  applies them to its CameraController (SdkCameraController in production).
- BridgeClient is the Linux brain's end: an async telemetry source *and* command
  sink on one connection.

This module deliberately imports no director/world code, so the transport can be used
without pulling the brain in. The driver loops that DO need the director (broadcast /
drive_live) live in show/live.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

import websockets

from ..settings import SHOW
from ..telemetry.frame import Frame, SessionInfo
from .command import CamCommand
from .controller import CameraController, LoggingController, apply

DEFAULT_PORT = SHOW.bridge_port

# What this build of the agent can actually do, advertised in the session message.
#
# The sim-box agent is a --noconsole exe deployed by hand, so it has no visible log and
# no version readout: the ONLY way to tell what is deployed has been to send a command
# and infer the build from whether anything happened. On 2026-07-25 that cost an
# evening: a stale exe silently swallowed ten diagnostic messages and the battery
# reported a confident (and completely wrong) verdict about the sim.
#
# Bump this whenever the op set changes. The Linux side reads it and can then say
# "that agent is too old for this" instead of guessing.
AGENT_PROTOCOL = 2


def controller_ops(controller) -> list[str]:
    """Which ops this controller can actually service, by inspection.

    Reported rather than assumed: a controller is a Protocol, so an older build simply
    lacks the newer methods, and that is exactly the thing we need to see from here.
    """
    from .command import CamOp

    checks = {
        CamOp.SWITCH_NUM: "switch_num", CamOp.SWITCH_POS: "switch_pos",
        CamOp.SET_STATE: "set_state", CamOp.REPLAY_SEEK: "replay_seek",
        CamOp.REPLAY_SEARCH: "replay_search", CamOp.REPLAY_SPEED: "replay_speed",
        CamOp.REPLAY_POS: "replay_pos", CamOp.RAW: "raw_broadcast",
    }
    return sorted(op for op, meth in checks.items() if callable(getattr(controller, meth, None)))


# --- sinks (director side) ------------------------------------------------
class CommandSink(Protocol):
    async def send(self, cmd: CamCommand) -> None: ...
    async def aclose(self) -> None: ...


class DirectSink:
    """Apply commands to a local controller (director + controller co-located)."""

    def __init__(self, controller: CameraController | None = None):
        self.controller = controller or LoggingController()

    async def send(self, cmd: CamCommand) -> None:
        apply(self.controller, cmd)

    async def aclose(self) -> None:
        pass


class WsCommandClient:
    """Ship commands to a remote BridgeServer over a WebSocket (command-only)."""

    def __init__(self, url: str):
        self.url = url
        self._ws = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.url)

    async def send(self, cmd: CamCommand) -> None:
        if self._ws is None:
            await self.connect()
        await self._ws.send(json.dumps(cmd.to_dict()))

    async def aclose(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


# --- server (sim-box side) ------------------------------------------------
class BridgeServer:
    def __init__(self, controller: CameraController | None = None, *, source=None,
                 host: str = SHOW.bind_host, port: int = DEFAULT_PORT,
                 rate: float | None = None,
                 record_path: str | None = None, record_hz: float | None = None):
        self.controller = controller or LoggingController()
        self.source = source           # a TelemetrySource to stream out (optional)
        self.host = host
        self.port = port
        self.rate = rate               # frames/sec pacing (None = as fast as the source yields)
        self.pump_error: BaseException | None = None   # why the last pump died, if it did
        # Tee every pumped frame to disk. The pump is a single shared read fanned out to
        # all clients, so recording is just one more consumer of it: no second reader
        # of the (single-consumer) live mmap, which is what used to make this awkward.
        self.record_path = record_path
        self.record_hz = record_hz     # written into the recording header as sample_rate
        self.recorded = 0
        self.clients: set = set()
        self._pump_task = None
        # the director's current shot (kind/target/pair), echoed to overlays on the
        # frame stream so they show the real on-camera car, not a simulated one
        self.current_shot: dict | None = None
        # Live SHOW config: settings that change mid-broadcast, such as which cars to
        # favour. Held here because the bridge is already the one thing every worker is
        # connected to, so a setting reaches the director and the tower over the socket
        # they each already have.
        #
        # Last-value-cached and replayed to every client on connect, which is what makes
        # it survive a worker restart. Without that, the supervisor bouncing a worker
        # would silently unmute it, and the director coming back would forget the VIPs
        # mid-race: the settings would quietly decay every time something restarted.
        self.show_config: dict = {}

    async def _handler(self, ws) -> None:
        # A streaming source: send this client the session context, then let the
        # shared pump broadcast frames to every client (single read, fan-out), so
        # the director and the overlay share one live stream without fighting over
        # the mmap. Register before starting the pump so no frame is missed.
        if self.source is not None:
            info = self.source.session_info()
            await ws.send(json.dumps({
                "type": "session", "session_info": info.raw,
                "agent": {"protocol": AGENT_PROTOCOL,
                          "controller": type(self.controller).__name__,
                          "ops": controller_ops(self.controller)},
            }))
            self.clients.add(ws)
            self._ensure_pump()
        else:
            self.clients.add(ws)
        # Replayed to every arrival, which is what makes a restarted worker come back
        # with the show still configured rather than with the defaults.
        if self.show_config:
            await ws.send(json.dumps({"type": "showconfig", "config": self.show_config}))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue  # ignore malformed frames rather than dropping the client
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "showconfig":
                    patch = msg.get("config")
                    if isinstance(patch, dict):
                        # MERGED, not replaced. The dock sets one field at a time, so a
                        # replace would make changing one setting wipe every other.
                        self.show_config.update(patch)
                        # Fanned out to EVERY client including the sender, so the dock
                        # learns the merged truth rather than only what it just sent.
                        await self._broadcast(json.dumps(
                            {"type": "showconfig", "config": self.show_config}))
                    continue
                if msg.get("type") == "shot":
                    # director telling us what it cut to; remembered and fanned out
                    # to overlays on the next frame (single writer = the pump).
                    self.current_shot = {"kind": msg.get("kind"), "target": msg.get("target"),
                                         "pair": msg.get("pair"), "flavor": msg.get("flavor")}
                    continue
                if not ("op" in msg or msg.get("type") == "cmd"):
                    continue
                try:
                    cmd = CamCommand.from_dict(msg)
                except (KeyError, TypeError):
                    continue
                try:
                    result = apply(self.controller, cmd)
                    if result is not None:
                        # A diagnostic raw broadcast: hand the Win32 outcome straight
                        # back to whoever asked. Commands are otherwise fire-and-forget,
                        # and that is exactly why the replay failure was so hard to pin.
                        await ws.send(json.dumps({"type": "cmd_result", **result}))
                except (ValueError, AttributeError, TypeError) as e:
                    # An op this agent is too old to know, or a controller missing the
                    # method for it. The agent exe on the sim box is deployed by hand and
                    # routinely lags the Linux brain, so this WILL happen every time the
                    # protocol grows. It must stay a logged no-op: `apply` raising here
                    # would escape the handler, close the socket, and take the telemetry
                    # stream down with it: losing the whole broadcast's data over one
                    # unrecognised camera command.
                    print(f"bridge: ignoring command {cmd.op!r} ({type(e).__name__}: {e})")
        finally:
            self.clients.discard(ws)

    def _ensure_pump(self) -> None:
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())

    def start_streaming(self) -> None:
        """Start reading the source now, without waiting for a client to connect.

        Recording must not depend on anyone being connected: the point of capturing a
        session is that it happens whether or not the Linux brain is up."""
        if self.source is not None:
            self._ensure_pump()

    def _open_recorder(self):
        if not self.record_path:
            return None
        from ..telemetry.recording import RecordingWriter

        writer = RecordingWriter(
            self.record_path, self.source.session_info(),
            sample_rate=self.record_hz or self.rate or 0.0, source="bridge")
        writer.open()
        return writer

    async def _pump(self) -> None:
        """Read the source once and broadcast each frame to all connected clients.

        The source is a synchronous iterator (and LiveSource sleeps between frames),
        so each step runs in the default executor to avoid stalling the event loop.
        A late joiner gets a contiguous tail, which is correct for a live stream.

        With record_path set, each frame is also appended to a recording. That write
        goes through the executor too: gzip plus a disk flush is blocking work, and a
        slow disk must never stall telemetry to the director.
        """
        loop = asyncio.get_running_loop()
        frames = self.source.frames()
        writer = self._open_recorder()
        self.pump_error = None
        try:
            while True:
                fr = await loop.run_in_executor(None, next, frames, None)
                if fr is None:
                    break
                if writer is not None:
                    await loop.run_in_executor(None, writer.write_frame, fr)
                    self.recorded = writer.count
                await self._broadcast(json.dumps({
                    "type": "frame", "t": fr.tick, "st": fr.session_time, "v": dict(fr.values),
                    "shot": self.current_shot,
                }))
                if self.rate:
                    await asyncio.sleep(1.0 / self.rate)
        except Exception as e:  # noqa: BLE001 - the pump's death has to be VISIBLE
            # An exception in a task nobody awaits is a silent hang for every client:
            # the sockets stay open, keepalive pings are answered by the library, and the
            # director sits in `async for` for the rest of the race while the studio's
            # port probe calls the bridge healthy. Say so, then end the stream like a
            # source that ran out, which every client already knows how to reconnect from.
            self.pump_error = e
            print(f"bridge: telemetry pump died ({type(e).__name__}: {e}); "
                  f"ending the stream so clients reconnect", flush=True)
        finally:
            if writer is not None:
                await loop.run_in_executor(None, writer.close)
        await self._broadcast(json.dumps({"type": "end"}))

    async def _broadcast(self, msg: str) -> None:
        # One client must not hold the frame for the others. A connection's `send` blocks
        # once its write buffer is past the high-water mark (32 KiB in websockets 16; a
        # frame is ~17 KB on a 28-car grid), so awaiting each client in turn let a wedged
        # overlay or a suspended preview tab stall the director after two frames.
        # broadcast() writes without waiting: a client that stops reading piles up in
        # its own buffer until the keepalive (see ws_server) closes it.
        websockets.broadcast(self.clients, msg)

    def ws_server(self):
        """The websockets.serve context manager (used by serve_forever and tests).

        Keepalive is tight on purpose: with a non-blocking fan-out a dead reader costs
        memory for as long as it lives, and 20 seconds is long enough on a LAN."""
        return websockets.serve(self._handler, self.host, self.port,
                                ping_interval=10, ping_timeout=10)

    async def serve_forever(self) -> None:
        shown = "localhost" if self.host in ("0.0.0.0", "") else self.host
        async with self.ws_server():
            print(f"camera bridge listening on ws://{shown}:{self.port}")
            print(f"  controller: {type(self.controller).__name__}")
            if self.source is not None:
                print(f"  streaming telemetry from: {type(self.source).__name__}")
            if self.record_path:
                print(f"  recording to: {self.record_path}")
                self.start_streaming()  # capture from now, not from the first client
            await asyncio.Future()


# --- client (Linux brain side) --------------------------------------------
async def fetch_session_info(url: str) -> SessionInfo:
    """One-shot: connect, take the session-info message, hang up.

    Cheap way for a long-lived client to re-read DriverInfo (team driver swaps
    rename cars mid-race) without interrupting its streaming connection - the
    bridge re-reads the source's session info for every new client."""
    client = BridgeClient(url)
    try:
        return await client.connect()
    finally:
        await client.aclose()


class BridgeClient:
    """Async telemetry source + command sink over one WebSocket to a BridgeServer.

    The director on the Linux brain uses this: `frames()` streams telemetry in,
    `send()` ships camera commands back, on a single connection.
    """

    def __init__(self, url: str, connect_timeout: float = 10.0, read_timeout: float = 30.0):
        self.url = url
        self.connect_timeout = connect_timeout
        # Longest silence before frames() gives up. The bridge streams at 10 Hz and
        # ends the stream itself when its source runs out or dies, so a silent socket is
        # a server that is alive and stuck; the driver loops treat the TimeoutError as a
        # dropped bridge and reconnect.
        self.read_timeout = read_timeout
        self._ws = None
        self.info: SessionInfo | None = None
        self.latest_shot: dict | None = None  # last shot echoed by the server (overlay side)
        # What the agent on the other end says it can do. None from a build that
        # predates the handshake carrying it, which is itself the useful signal.
        self.agent: dict | None = None
        # Live show config, kept current by frames(). Starts EMPTY rather than as a set
        # of defaults: an empty dict means "the server has not told us yet", and a
        # consumer that cannot tell that apart from "the operator cleared the VIPs"
        # would apply its own defaults over the top of a real setting.
        self.show_config: dict = {}
        self.on_show_config = None  # optional callback(dict) for live updates

    async def connect(self) -> SessionInfo:
        self._ws = await websockets.connect(self.url)
        raw = await asyncio.wait_for(self._ws.recv(), timeout=self.connect_timeout)
        msg = json.loads(raw)
        if msg.get("type") != "session":
            raise ValueError(f"expected a session message first, got {msg.get('type')!r}")
        self.info = SessionInfo(msg.get("session_info") or {})
        self.agent = msg.get("agent")
        return self.info

    def agent_supports(self, op: str) -> bool:
        """Can the agent service this op? Unknown (an agent too old to say) is treated
        as NO for anything beyond the original camera ops, so a caller that checks gets
        a straight answer instead of discovering it from silence."""
        from .command import CamOp

        if self.agent is None:
            return op in (CamOp.SWITCH_NUM, CamOp.SWITCH_POS, CamOp.SET_STATE)
        return op in (self.agent.get("ops") or [])

    async def frames(self):
        """Async generator of streamed frames, ending on the server's 'end'."""
        while True:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self.read_timeout)
            except websockets.ConnectionClosedOK:
                return
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "frame":
                self.latest_shot = msg.get("shot")
                yield Frame(tick=msg["t"], session_time=msg["st"], values=msg["v"])
            elif kind == "showconfig":
                # Not yielded: this rides the same socket as telemetry but is not a
                # frame, and a caller iterating frames() must not have to know that.
                self.show_config = msg.get("config") or {}
                if self.on_show_config is not None:
                    self.on_show_config(self.show_config)
            elif kind == "end":
                return

    async def send_show_config(self, patch: dict) -> None:
        """Set part of the show config. The server merges and fans the result back."""
        await self._ws.send(json.dumps({"type": "showconfig", "config": patch}))

    async def send(self, cmd: CamCommand) -> None:
        await self._ws.send(json.dumps({"type": "cmd", **cmd.to_dict()}))

    async def send_shot(self, shot) -> None:
        """Tell the bridge what the director just cut to, so overlays can show the
        real on-camera car(s). `shot` is a director.model.Shot."""
        await self._ws.send(json.dumps({
            "type": "shot", "kind": shot.kind, "target": shot.target_idx,
            "pair": list(shot.pair) if shot.pair else None, "flavor": shot.flavor,
        }))

    async def aclose(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
