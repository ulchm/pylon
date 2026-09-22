"""The driver loops: run the director and turn its cuts into camera commands.

This is the layer that runs the show. It composes the world model, the director, the
replay machine, the actuator, the bridge transport and the shot hand-off files, and it
sits ABOVE both director/ and camera/: it imports them, and nothing in them imports it
back. It used to live in camera/ and be re-exported from there, which made importing
the bridge transport load the whole brain (tests/test_camera.py now checks it does not).

- drive_live(client): the Linux-brain live loop. Frames arrive async from a
  BridgeClient; the world model + director are the same synchronous per-frame code
  as everywhere else; cuts ship straight back on the same socket.
- drive_live_forever(url): the same loop wrapped in connect/retry, which is what a
  long race actually needs.
- broadcast(source, sink): the local-source loop (dev-against-recordings, or the
  director co-located with the sim), pushing cuts to any CommandSink.
"""

from __future__ import annotations

import asyncio
import time

import websockets

from ..camera.actuator import Actuator
from ..camera.bridge import BridgeClient, CommandSink, fetch_session_info
from ..camera.command import CamCommand, CameraState, RpySrchMode
from ..camera.watch import DEBOUNCE, CameraWatch
from ..director import Director, DirectorConfig, ReplayDirector, ReplayState
from ..director.model import Decision, Shot, ShotFlavor, ShotKind
from ..director.replay import tape_time
from ..telemetry.frame import SessionInfo
from ..world import WorldModel
from ..world import run as run_world
from .shotlink import write_shot

# Re-publish the held shot at least this often (WALL seconds) so the overlay hand-off
# never goes stale during a long hold. Must be comfortably under shotlink.STALE_AFTER
# (15s), and it is measured on the same clock as that staleness: a paused tape freezes
# session time, and a heartbeat counted in session time would stop with it while the
# reader's wall clock carried on to "stale".
SHOT_HEARTBEAT = 4.0

# Re-read the bridge's session info this often (seconds). DriverInfo is a moving
# target: a car that joins after we connected has no number in the actuator, so every
# shot of it is dropped (command_for -> None) while the director believes it has cut;
# and in a team race every driver swap renames a car. 0 disables.
INFO_REFRESH = 60.0

# A dropped bridge is normal over a 6h race (the sim box restarts the agent, the LAN
# hiccups, iRacing reloads a session). These are the failures worth retrying rather
# than dying on. ValueError covers a handshake that didn't start with a session
# message, e.g. reconnecting to an agent mid-restart.
RETRYABLE = (TimeoutError, OSError, ValueError, websockets.WebSocketException)


def replay_banner(replay, *, phase: str = "rolling") -> dict | None:
    """What another process needs to know about a replay, from the marked candidate.

    Kept to plain data (no Shot, no CamCommand) because it crosses to other processes as
    JSON, and nothing downstream should need the director's types.

    Two readers, two phases:
      "armed"    a moment is marked and we are waiting for a lull. The OVERLAY acts on
                 this, preparing whatever it will show over the replay.
      "rolling"  the tape has actually moved and this is on air.

    `active` is true only when rolling, because it is what the overlay draws its
    treatment from: an armed candidate that never rolls must never have put letterbox
    bars on the broadcast.
    """
    cand = getattr(replay, "candidate", None)
    if cand is None:
        return None
    return {
        "active": phase == "rolling",
        "phase": phase,
        "key": cand.key,
        "kind": cand.kind,                       # contact | trouble | lead_change
        "label": cand.label,
        "car_number": cand.car_number,
        "driver": cand.driver,
        "position": cand.position,
        "other_number": cand.other_number,
        "other_driver": cand.other_driver,
        # Whether the viewer already saw this live changes what the caption should say:
        # "here it is again" reads differently from "you missed this".
        "missed_live": not cand.seen_on_camera,
        # WALL seconds from the tape landing to the marked instant, so a reader can
        # time its payoff line to land after the moment rather than over it. Not just
        # lead_in: the slow-motion ramp stretches the last `slow_lead` of the run-up to
        # roughly double, so the moment arrives later in wall time than in tape time.
        "moment_in": _moment_in(getattr(replay, "cfg", None)),
    }


def _moment_in(cfg) -> float:
    if cfg is None:
        return 0.0
    lead_in = float(getattr(cfg, "lead_in", 0.0))
    if not getattr(cfg, "slow_at_moment", False):
        return lead_in
    slow_lead = float(getattr(cfg, "slow_lead", 0.0))
    return lead_in + slow_lead      # that stretch plays at ~0.5x, so it costs double


def replay_commands(moves) -> list[CamCommand]:
    """Translate the replay machine's intent into wire commands.

    The same seam as Shot -> CamCommand: the director says what should happen to the
    tape, this says how to ask the sim for it. `to_end` is only ever produced from an
    explicit to_end move, never inferred, because on a saved tape it means the finish.
    """
    out: list[CamCommand] = []
    for m in moves:
        if m.kind == "seek":
            out.append(CamCommand.replay_seek(m.session_num, m.session_time, label=m.label))
        elif m.kind == "to_end":
            out.append(CamCommand.replay_search(RpySrchMode.TO_END, label=m.label))
        elif m.kind == "speed":
            out.append(CamCommand.replay_speed(m.speed, slow_motion=m.slow_motion,
                                               label=m.label))
    return out


def _new_director(cfg: DirectorConfig | None) -> Director:
    """The Director every driver loop runs on.

    One constructor for all of them, so a loop can never ship with a piece of wiring
    the others have: `pylon live`, the one the studio runs, once shipped a camera hold
    that never held because it was wired into broadcast() alone.
    """
    return Director(cfg)


async def drive_live(
    client: BridgeClient,
    *,
    cfg: DirectorConfig | None = None,
    actuator: Actuator | None = None,
    limit: float = 0.0,
    takeover: bool = False,
    angle_refresh: float = 9.0,
    camera_debounce: float = DEBOUNCE,
    on_decision=None,
    on_snapshot=None,
    on_camera=None,
    replay: ReplayDirector | None = None,
    on_replay=None,
    refresh_every: float = INFO_REFRESH,
    clock=time.monotonic,
) -> int:
    """Run the director against telemetry streamed from a bridge, cutting live.

    angle_refresh: while HOLDING one subject, re-frame it from a fresh camera angle this
    often (session seconds) so a long battle/trouble hold isn't one flat static camera.
    0 disables. camera_debounce: how long the sim's camera may disagree with the shot
    before the command is re-sent (see CameraWatch); 0 disables the check entirely.
    refresh_every: see INFO_REFRESH. clock: wall time, injectable for tests.

    Returns the number of commands sent.
    """
    info = client.info or SessionInfo({})
    wm = WorldModel(info)
    director = _new_director(cfg)
    actuator = actuator or Actuator(info)
    watch = CameraWatch(camera_debounce) if camera_debounce > 0 else None

    async def refresh_loop() -> None:
        # The same throwaway second connection the overlay uses; the
        # bridge re-reads its source's session info for every new client.
        while True:
            await asyncio.sleep(refresh_every)
            try:
                fresh = await fetch_session_info(client.url)
            except RETRYABLE:
                continue                  # bridge hiccup; next round
            wm.refresh_info(fresh)
            actuator.refresh_info(fresh)

    refresher = asyncio.create_task(refresh_loop()) if refresh_every > 0 else None
    try:
        return await _drive(client, wm, director, actuator, limit=limit, takeover=takeover,
                            angle_refresh=angle_refresh, on_decision=on_decision,
                            on_snapshot=on_snapshot, replay=replay, on_replay=on_replay,
                            watch=watch, on_camera=on_camera, clock=clock)
    finally:
        if refresher is not None:
            refresher.cancel()


async def _drive(client, wm, director, actuator, *, limit, takeover, angle_refresh,
                 on_decision, on_snapshot, replay, on_replay, watch, on_camera,
                 clock) -> int:
    if takeover:
        await client.send(CamCommand.set_state(CameraState.UI_HIDDEN,
                                               label="takeover: hide UI, manual cam"))
    n = 0
    last_angle = -1e9      # session time: a render cadence, and it pauses with the tape
    last_echo = -1e9       # wall time: the reader's staleness is wall time too
    async for frame in client.frames():
        # THE REPLAY ISOLATION (#18). While the sim's tape is elsewhere, its telemetry
        # describes the replayed moment, not the live race. Those frames simply never
        # reach the world model: no gate to get wrong, no second model to leak into
        # SessionMemory, and no chance of re-detecting an overtake that already
        # happened. `wm` holds exactly the state it had when we left.
        back_live = False      # is this the first live frame after an excursion?
        if replay is not None and replay.rolling:
            cand, failed_before = replay.candidate, replay.failed_seeks
            for cmd in replay_commands(replay.tick(frame, clock())):
                await client.send(cmd)
                n += 1
            if replay.failed_seeks > failed_before:
                # The seek was never honoured and `tick` gave the excursion up. Say so:
                # on air this used to look exactly like a working replay that cut away
                # at once, and nothing in the log said otherwise (Round 1, 2026-09-20).
                # The frames since the seek went out were live and never fed, and the
                # camera is on the candidate's car, so it is a return like any other.
                if on_replay is not None:
                    on_replay("failed", cand)
                write_shot(director.current, replay=None)
                back_live = True
            else:
                # Tell the overlay we are showing the past, every frame we are away. It
                # cannot infer this: the overlay reads the same bridge we do, and during
                # an excursion those frames look like ordinary live telemetry to it.
                # Republished per frame rather than once at the start so the banner
                # survives an overlay reload mid-replay and expires on its own if this
                # loop dies.
                write_shot(director.current,
                           replay=replay_banner(replay, phase="rolling"))
                if not replay.arrived(frame):
                    continue
                replay.finish(frame.session_time)
                write_shot(director.current, replay=None)   # back to live: clear it
                if on_replay is not None:
                    on_replay("end", None)
                back_live = True
        if back_live:
            # The excursion pointed the camera somewhere else on purpose, and the
            # frame we return on would otherwise read as a drift and re-assert
            # instantly: breaking the isolation from the far side.
            if watch is not None:
                watch.reset()
            # The frame after an excursion is not continuous with the one before it,
            # and `_detect_jump` cannot see that (on a tape we land within a second of
            # where we left). Without this, the first live frame derives speed across
            # the whole excursion and a progress delta can wrap a lap.
            wm.resync()
            # deliberately no `continue`: this frame IS live again, so it gets directed
            # on like any other rather than being thrown away for arriving at an
            # awkward moment. The camera is put back on the live shot BELOW, after the
            # director has had this frame, and exactly once: see the hold branch.

        if replay is not None and not replay.rolling:
            # Which production path is this? Read the RAW channel, not
            # snap.session.is_replay, which folds "absent" into False, and here the
            # difference decides how we come home. A feed with no IsReplayPlaying must
            # stay None (unknown -> the safe seek-to-mark), never False (a live edge ->
            # to_end), because on a saved tape to_end means the finish.
            flag = frame.get("IsReplayPlaying")
            replay.source_is_tape = None if flag is None else bool(flag)

        snap = wm.update(frame)
        # Every live snapshot, before any director logic: this is the seam the
        # OBS scene switcher hangs off. It gets the world's view of the session
        # rather than the director's view of the camera, because what belongs on
        # the switcher is a question about the session, not about the shot.
        if on_snapshot is not None:
            on_snapshot(snap)
        if limit and snap.session_time > limit:
            break
        dec: Decision | None = director.update(snap)
        t = snap.session_time
        if dec is not None:
            cmd = actuator.command_for(dec.shot, session_time=dec.time)
            if cmd is not None:
                await client.send(cmd)
                await client.send_shot(dec.shot)  # echo the shot so overlays show the real car(s)
                write_shot(dec.shot)              # + local hand-off, for overlays sharing this box
                last_angle, last_echo = t, clock()
                n += 1
                if on_decision is not None:
                    on_decision(dec, cmd)
        elif director.current is not None:
            # No subject change this frame, but we may be HOLDING one for a long time (a
            # stricken car, or a battle we're staying with for laps).
            #
            # Or we have just come home from a replay, which left the camera on the
            # replayed car; the sim keeps it there after the seek home, and left to the
            # drift watch that is a debounce (2.6s on air) of live pictures of the wrong
            # car after every replay. So the live shot is re-sent NOW, but only when
            # the director did not cut on this frame. ONE camera command per frame: the
            # sim honours the first switch and drops a second sent right behind it
            # (measured over 15 of Round 1's 16 replays: the re-assert stuck, the
            # director's same-frame cut never landed, and the drift watch re-sent it
            # 2.5s later while the overlay already named the new car).
            if back_live or (angle_refresh > 0 and t - last_angle >= angle_refresh):
                # Re-frame the SAME car from the next angle so the hold doesn't sit on one
                # flat look. This is a render-domain angle change, not a director cut.
                cmd = actuator.command_for(director.current, session_time=t)
                if cmd is not None:
                    await client.send(cmd)
                    last_angle, last_echo = t, clock()
                    n += 1
            if clock() - last_echo >= SHOT_HEARTBEAT:
                # Keep the overlay hand-off fresh; otherwise it goes stale and the
                # pop-in has nothing current to show for a camera that has not moved.
                await client.send_shot(director.current)
                write_shot(director.current)
                last_echo = clock()

        # Did the picture actually go where we asked (#68)? Outside the cut/hold
        # branches above, because a shot can drift whether or not it just changed,
        # and after them, so a re-assert this frame is the LAST word on the camera
        # rather than something the hold's angle refresh overwrites. Never reached
        # while the tape is rolling: that branch continues above, and the frame it
        # returns on has just reset the watch.
        if watch is not None:
            drift = watch.observe(snap, director.current, t=t)
            if drift is not None:
                cmd = actuator.command_for(drift.shot, session_time=t)
                if cmd is not None:
                    await client.send(cmd)
                    last_angle, last_echo = t, clock()
                    n += 1
                    if on_camera is not None:
                        on_camera(drift, cmd)

        if replay is not None:
            replay.observe(snap, director.current)
            for idx in director.trouble:
                on_cam = director.current is not None and director.current.target_idx == idx
                replay.note_trouble(snap, idx, on_cam, since=director.trouble_since(idx))
            # Publish the ARMED candidate as well as the rolling one, so the overlay
            # knows a replay is coming before it cuts. The wait for a lull is normally
            # seconds, and anything preparing graphics for the moment gets that time.
            if replay.state == ReplayState.ARMED:
                write_shot(director.current,
                           replay=replay_banner(replay, phase="armed"))
            cmds = replay_commands(replay.maybe_start(snap, director.current, clock(),
                                                      trouble=director.trouble,
                                                      tape_now=tape_time(frame),
                                                      shot_since=director.started))
            if cmds:
                cand = replay.candidate
                # Point at the car involved before rolling. The AngleRotator never
                # repeats a group back to back, so the replay is framed differently
                # from the live shot by construction (#18: "an angle deliberately
                # different from the live one"). ShotFlavor.REPLAY keeps it to the
                # trackside TV cameras.
                #
                # WHICH car: the one that came off worst. The world model frames a
                # contact on the better-placed car, which is right for a live cut and
                # wrong for a replay: the TV camera then follows a car driving away
                # while the other one spins behind it, out of shot. By now (wreck_dwell
                # later) the director's latch says which of them is stricken.
                idx, number, name = cand.car_idx, cand.car_number, cand.driver
                if (cand.other_idx is not None and cand.other_idx in director.trouble
                        and cand.car_idx not in director.trouble):
                    idx, number, name = cand.other_idx, cand.other_number, cand.other_driver
                look = actuator.command_for(
                    Shot(ShotKind.INCIDENT, f"replay:{idx}", idx,
                         f"#{number} {name}".rstrip(), flavor=ShotFlavor.REPLAY),
                    session_time=cand.at)
                for cmd in ([*cmds, look] if look is not None else cmds):
                    await client.send(cmd)
                    n += 1
                if on_replay is not None:
                    on_replay("start", cand)
    return n


async def drive_live_forever(
    url: str,
    *,
    cfg: DirectorConfig | None = None,
    actuator_factory=None,
    limit: float = 0.0,
    takeover: bool = False,
    angle_refresh: float = 9.0,
    camera_debounce: float = DEBOUNCE,
    on_decision=None,
    on_snapshot=None,
    on_camera=None,
    on_connect=None,
    on_disconnect=None,
    reconnect: float = 2.0,
    replay: ReplayDirector | None = None,
    on_replay=None,
) -> int:
    """Run the director against a bridge, reconnecting for as long as we're alive.

    The director is the one worker a broadcast cannot afford to lose: the overlay
    going quiet is a missing tower, but the director going quiet leaves the camera
    frozen wherever it last pointed, for the rest of the race. The overlay and
    the bridge and overlay workers already self-heal; this brings the brain in line.

    Each connection is a fresh WorldModel/Director/Actuator, which is what we want:
    the world model's speed and closing rates are derived from frame-to-frame deltas,
    so carrying state across a gap of unknown length would feed the director garbage
    for the first few ticks. `actuator_factory(session_info)` builds the actuator so
    the angle personality survives the reconnect.

    Returns the total number of commands sent across all connections. Only returns on
    its own when `limit` is set (a dev/test bound); otherwise cancel it to stop.
    """
    total = 0
    while True:
        client = BridgeClient(url)
        try:
            info = await client.connect()
            if on_connect is not None:
                on_connect(info)
            actuator = actuator_factory(info) if actuator_factory is not None else None
            if replay is not None:
                replay.reset_connection()
            total += await drive_live(
                client, cfg=cfg, actuator=actuator, limit=limit, takeover=takeover,
                angle_refresh=angle_refresh, camera_debounce=camera_debounce,
                on_decision=on_decision, on_snapshot=on_snapshot, on_camera=on_camera,
                replay=replay, on_replay=on_replay,
            )
            if limit:
                return total
            reason = "telemetry stream ended"
        except RETRYABLE as e:
            reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        finally:
            await client.aclose()
        if on_disconnect is not None:
            on_disconnect(reason)
        await asyncio.sleep(reconnect)


async def broadcast(
    source,
    sink: CommandSink,
    *,
    cfg: DirectorConfig | None = None,
    actuator: Actuator | None = None,
    limit: float = 0.0,
    takeover: bool = False,
    on_decision=None,
) -> int:
    """Run the director over a *local* source and push cuts to a sink.

    takeover: send a set_state first that hides the UI and (by omitting the
    auto-shot bit) hands camera control to us rather than iRacing's auto director.
    Returns the number of commands sent.
    """
    info = source.session_info()
    actuator = actuator or Actuator(info)
    director = _new_director(cfg)

    if takeover:
        await sink.send(CamCommand.set_state(CameraState.UI_HIDDEN,
                                             label="takeover: hide UI, manual cam"))

    n = 0
    for snap in run_world(source):
        if limit and snap.session_time > limit:
            break
        dec: Decision | None = director.update(snap)
        if dec is None:
            continue
        cmd = actuator.command_for(dec.shot, session_time=dec.time)
        if cmd is None:
            continue
        await sink.send(cmd)
        n += 1
        if on_decision is not None:
            on_decision(dec, cmd)
    return n
