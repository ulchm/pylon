"""pylon command line.

The two commands almost everyone uses:

    pylon studio                    run the whole show, supervised (the one launcher)
    pylon obs-setup                 build the OBS scenes: capture, overlay, cards

Setting up and checking:

    pylon config [--edit|--path]    show or open the settings file
    pylon obs-prepare               switch obs-websocket on and add the control panel
    pylon doctor                    check OBS, iRacing, the scenes and the ports

The workers, which the studio runs for you:

    pylon bridge   [--sdk] [--live | --replay FILE] [--record OUT]
    pylon live     ws://127.0.0.1:8779      the director
    pylon overlay  --bridge ws://...        the timing tower

Offline, against a recording:

    pylon synth    OUT.jsonl.gz [--cars N --seconds S --hz H --seed K]
    pylon describe FILE            (recording, or .ibt for a quick channel report)
    pylon replay   FILE [--top N --limit S]      running-order preview (raw frames)
    pylon story    FILE [--battles N --limit S]  world-model story (battles, events)
    pylon direct   FILE                          dry-run the director, print the shots
    pylon overlay  FILE                          preview the tower
    pylon broadcast FILE|--live                  director to cameras from a local source
    pylon record   OUT.jsonl.gz                  live capture (Windows only)
"""

from __future__ import annotations

import argparse

from .config import load as load_config
from .director import DirectorConfig, ReplayConfig
from .obs import OutputSettings, SceneConfig
from .settings import SHOW
from .telemetry import PlaybackSource, RecordingWriter, SyntheticSource
from .telemetry.constants import SessionFlag

# The camera styles a person picks by name. The internal names still work (see
# camera/angles.POLICIES) but are not offered here: a choices list is a menu, and
# a menu with two names for the same thing is a worse menu.
ANGLE_CHOICES = ("tv", "onboard", "wide")


#: The operator's settings, read once so argparse can default from them. A flag
#: still wins over the file: the file is what the show normally runs on, and a flag
#: is someone overriding it for one run.
_cfg = load_config()


def _angles_default() -> str:
    """The configured camera style, or "tv" when it names one that does not exist.

    Falling back rather than failing is the point: a typo in a settings file must
    not stop a broadcast, and `pylon doctor` is where it gets reported.
    """
    want = _cfg.director.angles.strip().lower()
    return want if want in ANGLE_CHOICES else "tv"


def _log_decision(actuator):
    """A cut logger that names the camera angle (group -> name) so the variety shows."""
    names = {num: name for name, num in actuator.cmap.by_name.items()}

    def on_decision(dec, cmd) -> None:
        target = f"#{cmd.car_number}" if cmd.car_number else f"pos {cmd.position}"
        angle = names.get(cmd.group, f"group {cmd.group}")
        print(f"[t={dec.time:7.1f}s]  {dec.shot.kind.upper():<8} {dec.shot.label:<36} "
              f"-> {target:<7} {angle}")

    return on_decision


def _log_camera(actuator):
    """A drift logger: the camera went somewhere the director did not ask for (#68).

    The whole cost of the Oulton Park incident was that the broadcast looked wrong
    for a long time with nothing anywhere reporting it, so this prints loudly and
    says both car indices: the point of the line is that an operator reading the
    director's output can see the loop working, or not.
    """
    names = {num: name for name, num in actuator.cmap.by_name.items()}

    def on_camera(drift, cmd) -> None:
        angle = names.get(cmd.group, f"group {cmd.group}")
        target = f"#{cmd.car_number}" if cmd.car_number else f"pos {cmd.position}"
        print(f"[t={drift.at:7.1f}s]  CAMERA   drifted to car {drift.actual} "
              f"after {drift.held:.1f}s; re-asserting {drift.shot.kind} "
              f"{drift.shot.label!r} -> {target} {angle}")

    return on_camera


def cmd_synth(args: argparse.Namespace) -> int:
    src = SyntheticSource(
        num_cars=args.cars, duration_s=args.seconds, hz=args.hz, seed=args.seed
    )
    with RecordingWriter(args.out, src.session_info(), sample_rate=args.hz, source="synthetic") as w:
        for fr in src.frames():
            w.write_frame(fr)
    print(f"wrote {w.count} frames -> {args.out}")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    path = args.file
    if path.endswith(".ibt"):
        import irsdk

        ibt = irsdk.IBT()
        ibt.open(path)
        try:
            names = ibt.var_headers_names
            caridx = [n for n in names if n.startswith("CarIdx")]
            rec = ibt._disk_header.session_record_count
            print(f"ibt: {path}")
            print(f"  channels: {len(names)} | records: {rec} | CarIdx channels: {len(caridx)}")
            if not caridx:
                print("  note: no CarIdx channels (driver .ibt has no multi-car data)")
        finally:
            ibt.close()
        return 0

    src = PlaybackSource(path)
    info = src.session_info()
    n = 0
    first = last = None
    caridx: set[str] = set()
    for fr in src.frames():
        n += 1
        if first is None:
            first = fr.session_time
            caridx = {k for k in fr.values if k.startswith("CarIdx")}
        last = fr.session_time
    dur = (last - first) if (first is not None and last is not None) else 0.0
    print(f"recording: {path}")
    print(f"  source: {src.header.get('source')} | frames: {n} | duration: {dur:.1f}s "
          f"| sample_rate: {src.sample_rate}")
    print(f"  track: {info.track_name} | length: {info.track_length_m()} m "
          f"| drivers: {len(info.drivers())}")
    print(f"  CarIdx channels: {len(caridx)} -> {sorted(caridx)}")
    if src.truncated:
        print("  ! truncated: the capture was cut short (killed mid-write). The frames "
              "above are intact and usable; only the tail is missing.")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    src = PlaybackSource(args.file)
    info = src.session_info()
    dmap = info.drivers_by_idx()
    print(f"track: {info.track_name} | drivers: {len(dmap)} | sample_rate: {src.sample_rate}")
    last_shown = -1e9
    for fr in src.frames():
        if args.limit and fr.session_time > args.limit:
            break
        if fr.session_time - last_shown < args.every:
            continue
        last_shown = fr.session_time

        pos = fr.get("CarIdxPosition") or []
        f2 = fr.get("CarIdxF2Time") or []
        active = [i for i, p in enumerate(pos) if p and p > 0]
        order = sorted(active, key=lambda i: pos[i])
        yellow = " [YELLOW]" if (fr.get("SessionFlags") or 0) & SessionFlag.YELLOW else ""
        print(f"\n[t={fr.session_time:6.1f}s]{yellow}")
        prev = None
        for i in order[: args.top]:
            d = dmap.get(i)
            label = f"#{d.number} {d.name}" if d else f"car{i}"
            interval = ""
            if prev is not None and f2[i] >= 0 and f2[prev] >= 0:
                gap = f2[i] - f2[prev]
                interval = f"  +{gap:0.2f}s" + ("  <<battle" if gap < 0.75 else "")
            print(f"  P{pos[i]:>2}  {label:<22}{interval}")
            prev = i
    return 0


def cmd_story(args: argparse.Namespace) -> int:
    from .world import render_story, run

    src = PlaybackSource(args.file)
    info = src.session_info()
    print(f"track: {info.track_name} | drivers: {len(info.drivers())}")
    last_shown = -1e9
    for snap in run(src):
        if args.limit and snap.session_time > args.limit:
            break
        periodic = (snap.session_time - last_shown) >= args.every
        if not snap.events and not periodic:
            continue
        last_shown = snap.session_time
        for line in render_story(snap, top_battles=args.battles):
            print(line)
    return 0


def cmd_direct(args: argparse.Namespace) -> int:
    from .director import Director, DirectorConfig
    from .director.model import ShotKind
    from .world import run as run_world

    cfg = DirectorConfig(min_shot=args.min_shot, max_shot=args.max_shot, cut_margin=args.cut_margin)
    src = PlaybackSource(args.file)
    info = src.session_info()
    print(f"track: {info.track_name} | drivers: {len(info.drivers())}")

    names = {
        ShotKind.LEADER: "LEADER", ShotKind.BATTLE: "BATTLE",
        ShotKind.INCIDENT: "INCIDENT", ShotKind.FOLLOW: "FOLLOW",
    }
    d = Director(cfg)
    time_by_kind: dict[str, float] = {}
    lengths: list[float] = []
    prev_kind = None
    cuts = 0
    last_t = 0.0

    for snap in run_world(src):
        last_t = snap.session_time
        if args.limit and snap.session_time > args.limit:
            break
        dec = d.update(snap)
        if dec is None:
            continue
        cuts += 1
        if prev_kind is not None:
            time_by_kind[prev_kind] = time_by_kind.get(prev_kind, 0.0) + dec.prev_held
            lengths.append(dec.prev_held)
        prev_kind = dec.shot.kind
        tag = names.get(dec.shot.kind, dec.shot.kind.upper())
        why = dec.reason + (f", prev {dec.prev_held:.1f}s" if dec.prev_held > 0 else "")
        print(f"[t={dec.time:7.1f}s]  {tag:<8}  {dec.shot.label:<36} score {dec.score:5.1f}  [{why}]")

    if prev_kind is not None:
        tail = last_t - d.started
        time_by_kind[prev_kind] = time_by_kind.get(prev_kind, 0.0) + tail
        lengths.append(tail)

    if lengths:
        total = sum(time_by_kind.values()) or 1.0
        avg = sum(lengths) / len(lengths)
        print("\n=== director summary ===")
        print(f"cuts: {cuts}  |  avg shot {avg:.1f}s  |  shortest {min(lengths):.1f}s  "
              f"|  longest {max(lengths):.1f}s")
        by = sorted(time_by_kind.items(), key=lambda x: -x[1])
        print("time by shot type: " + "   ".join(f"{names.get(k, k)} {100 * v / total:.0f}%" for k, v in by))
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    import asyncio

    from .overlay import serve, serve_live

    try:
        if args.bridge:
            asyncio.run(serve_live(args.bridge, host=args.host, ws_port=args.ws_port,
                                   http_port=args.http_port))
        elif args.file:
            asyncio.run(serve(args.file, host=args.host, ws_port=args.ws_port,
                              http_port=args.http_port, rate=args.rate, loop=not args.once))
        else:
            print(f"give a recording FILE, or --bridge {SHOW.bridge_url()} for a live feed")
            return 2
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show the settings file, open it, or print where it is."""
    import subprocess
    import sys

    from .config import ensure_config, to_toml

    cfg, path, created = ensure_config()
    if args.path:
        print(path)
        return 0
    if args.edit:
        # The platform's own "open this file" verb, so it lands in whatever the
        # person already uses for text rather than whatever we would have guessed.
        try:
            if sys.platform == "win32":
                subprocess.run(["cmd", "/c", "start", "", str(path)], check=False)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as e:
            print(f"could not open an editor ({e}); the file is at {path}")
            return 1
        print(f"opened {path}")
        return 0
    if created:
        print(f"wrote a new settings file at {path}\n")
    else:
        print(f"{path}\n")
    for problem in cfg.problems:
        print(f"! {problem}")
    print(to_toml(cfg), end="")
    return 0


def cmd_obs_prepare(args: argparse.Namespace) -> int:
    """Switch on obs-websocket and add the control panel, by editing OBS's own files.

    The half of setting up OBS that cannot be done over the WebSocket, because it is
    what makes the WebSocket reachable in the first place.
    """
    from .obs.prepare import prepare_obs
    from .settings import SHOW

    rep = prepare_obs(panel_url=args.panel_url or SHOW.control_url(),
                      panel_title=args.panel_title, force=args.force)
    for line in rep.lines:
        print(line)
    return 0 if rep.ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check everything a broadcast needs and say what to do about what is missing."""
    from .show.doctor import run as run_checks

    rep = run_checks(check_sim=not args.no_sim)
    for line in rep.lines():
        print(line)
    print()
    print("Ready to broadcast." if rep.ok else
          "Not ready: fix the FAIL lines above and run this again.")
    return 0 if rep.ok else 1


def cmd_studio(args: argparse.Namespace) -> int:
    """Bring the whole one-box studio up and keep it up."""
    import sys
    import time
    from pathlib import Path

    from .show.dock import serve_control
    from .show.studio import Studio, default_workers, format_event, rotate_log, supervise

    cfg = load_config()
    for problem in cfg.problems:
        print(f"[studio] config: {problem}", flush=True)
    # The config decides; the flags are the override for one run. That order is what
    # lets someone who never opens a terminal change the show, and still lets a
    # shortcut with --replay exercise the whole thing with no session running.
    replays = cfg.director.replays and not args.no_replays
    workers = default_workers(python=sys.executable, bridge_port=args.bridge_port,
                              replay=args.replay, replays=replays,
                              obs_scenes=cfg.obs.scenes and not args.no_obs_scenes)
    log_dir = None if args.no_worker_logs else str(args.log_dir or SHOW.log_dir)

    # The supervision log goes to a FILE as well as the console. The console alone is
    # not a record: cmd.exe gives it a 30-line scrollback, so by the time anyone asks
    # why a worker died, the answer has scrolled off, which is exactly how a two-hour
    # crash loop stayed undiagnosed on 2026-08-02.
    studio_log = None
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            rotate_log(Path(log_dir) / "studio.log")    # append-only would grow forever
            studio_log = open(Path(log_dir) / "studio.log", "a",  # noqa: SIM115
                              encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[studio] could not open the supervision log: {e}", flush=True)

    def emit(line: str) -> None:
        print(line, flush=True)
        if studio_log is not None:
            studio_log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
            studio_log.flush()

    studio = Studio(workers, on_status=lambda **kw: emit(format_event(**kw)), log_dir=log_dir)
    emit(f"[studio] starting {len(workers)} workers: "
         f"{', '.join(w.name for w in workers)}")
    if log_dir:
        emit(f"[studio] worker logs: {log_dir}")

    try:
        dock = None
        if not args.no_control:
            # Started BEFORE the workers, so the dock can show them coming up (and show
            # one that never does) rather than only appearing once everything is fine.
            try:
                dock = serve_control(studio, host=args.control_host, port=args.control_port)
            except OSError as e:
                # Almost always another studio, whose workers would now be doubled.
                emit(f"[studio] the control dock cannot bind "
                     f"{args.control_host}:{args.control_port} ({e}); is another studio "
                     f"already running? Nothing started.")
                return 1
            emit(f"[studio] control panel: "
                 f"http://{args.control_host}:{args.control_port}/  "
                 f"(in OBS: View > Docks > Custom Browser Docks)")
        supervise(studio, interval=args.interval, emit=emit, dock=dock)
    finally:
        if studio_log is not None:
            studio_log.close()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from .telemetry.live import record

    return record(args.out, duration_s=args.duration, hz=args.hz)


def cmd_bridge(args: argparse.Namespace) -> int:
    import asyncio

    from .camera import BridgeServer
    from .show.wiring import camera_controller, telemetry_source

    controller = camera_controller(sdk=args.sdk)
    try:
        source = telemetry_source(live=args.live, file=args.replay, hz=args.hz)
    except RuntimeError as e:
        print(e)
        return 1

    if args.record and source is None:
        print("--record needs a telemetry source: add --live (sim box) or --replay FILE")
        return 2

    # --hz is the LIVE poll rate; a --replay stream is paced by --rate instead. Record
    # whichever actually governs frame timing, so the header's sample_rate is honest.
    server = BridgeServer(controller, source=source, host=args.host, port=args.port,
                          rate=args.rate, record_path=args.record,
                          record_hz=args.hz if args.live else args.rate)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def cmd_obs_setup(args: argparse.Namespace) -> int:
    from .config import load as load_config
    from .obs import (
        OutputSettings,
        build_program_scene,
        connect,
        ensure_cards,
        ensure_output,
        ensure_stream_target,
    )
    from .settings import stream_settings
    from .show.obssetup import program_scene, scene_prefix, source_prefix

    # The names come from the operator's config, exactly as `live --obs-scenes` reads
    # them, so the scenes this builds are the ones the director later cuts to. They
    # were argparse constants once, and on a PC with a second broadcaster that meant
    # provisioning reached straight into the other show's programme scene.
    cfg = load_config()
    scene = args.scene if args.scene is not None else program_scene(cfg)
    prefix = args.scene_prefix if args.scene_prefix is not None else scene_prefix(cfg)
    sources = source_prefix(cfg)

    # The encode is pinned ONLY when we also set the destination. Encoder settings
    # live in the OBS PROFILE, outside any scene collection, so writing them because
    # somebody ran obs-setup reaches every other show on the machine. A Custom RTMP
    # destination still needs them, which is the case this exists for.
    stream = stream_settings()
    want_output = None if (args.no_output or not stream) else OutputSettings(
        bitrate=args.bitrate, keyint_sec=args.keyint, preset=args.nvenc_preset)

    def print_output(rep: dict) -> None:
        if rep["changed"]:
            state = "set: " + ", ".join(rep["changed"]) + "; restart OBS to apply"
        else:
            state = "already set"
        print(f"  stream output    : {rep['settings']} ({state})")

    try:
        cl = connect(host=args.host, port=args.port, password=args.password)
    except Exception as e:  # noqa: BLE001 - report any connect failure, don't traceback
        print(f"could not reach OBS websocket at {args.host}:{args.port} - is OBS running "
              f"with the WebSocket server enabled (Tools -> WebSocket Server Settings)?\n  {e}")
        # The output settings live in files OBS reads at startup, so a stopped OBS
        # is the one thing here that does not need the websocket, and the most
        # reliable moment to write them.
        if want_output is not None:
            rep = ensure_output(None, want_output)
            print_output(rep)
            for w in rep["warnings"]:
                print(f"  ! {w}")
        return 1

    report = build_program_scene(cl, scene=scene,
                                 overlay_url=args.overlay_url,
                                 game_window=args.game_window or "",
                                 capture_crop=args.capture_crop,
                                 source_prefix=sources,
                                 width=args.width, height=args.height, fps=args.fps)
    # Holding scenes are their own scenes, not part of Program, so they are
    # provisioned separately rather than folded into build_program_scene.
    cards = [] if args.no_cards else ensure_cards(cl, base_url=args.cards_url,
                                                  width=args.width, height=args.height,
                                                  prefix=prefix, source_prefix=sources,
                                                  warnings=report["warnings"])
    if report["game"]:
        how, blank = "local capture", "the sim is running and fullscreen"
    else:
        how, blank = "none", "there is a video source at all"
    print(f"scene '{report['scene']}' ready ({args.width}x{args.height}@{args.fps}):")
    print(f"  video source     : {report['video'] or 'MISSING'} ({how})")
    if report["crop"]:
        c = report["crop"]
        print(f"  capture crop     : {c['source'][0]}x{c['source'][1]} -> "
              f"{c['kept'][0]}x{c['kept'][1]}  (left {c['cropLeft']}, right {c['cropRight']}, "
              f"top {c['cropTop']}, bottom {c['cropBottom']})")
    print(f"  overlay source   : {report['overlay'] or 'MISSING'}")
    print(f"  holding scenes   : {', '.join(cards) or 'none'}")
    # The destination is provisioned with the scenes so a rebuilt OBS is a
    # one-command job; the key comes from the config and is never printed.
    if stream:
        st = ensure_stream_target(cl, server=stream.server, key=stream.key)
        state = "set" if st["changed"] else "already set"
        print(f"  stream target    : {st['server']} ({state}, key "
              f"{'present' if st['key_set'] else 'MISSING'})")
        report["warnings"].extend(st["warnings"])
    else:
        print("  stream target    : left as OBS has it (set obs.stream_server and "
              "obs.stream_key in config.toml to provision it here)")
    # The encode is pinned with the destination, for the same reason: Round 1 went
    # out on OBS's "auto" keyframe interval because nothing here said otherwise.
    if want_output is not None:
        out = ensure_output(cl, want_output)
        print_output(out)
        report["warnings"].extend(out["warnings"])
    for w in report["warnings"]:
        print(f"  ! {w}")
    print(f"look at OBS to confirm the overlay draws (the video pane stays black until "
          f"{blank}).")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    import asyncio

    from .show.live import drive_live_forever
    from .show.wiring import (
        actuator_for,
        director_config,
        obs_snapshot_hook,
        replay_director,
        scene_config,
    )

    # OBS scene switching is opt-in and best-effort; None means cameras only.
    #
    # The scene NAMES have to be the ones obs-setup built, and those carry the show's
    # own name so two broadcasters on one PC cannot touch each other's scenes. Derived
    # from the same config here rather than passed on the command line, so the studio
    # cannot spawn a director pointed at scenes nobody made.
    on_snapshot = None
    if args.obs_scenes:
        from .show.obssetup import program_scene, scene_prefix

        prefix = scene_prefix(_cfg)
        on_snapshot = obs_snapshot_hook(
            scene_cfg=scene_config(post_race_hold=args.post_race_hold,
                                   chequer_max_hold=args.chequer_max_hold,
                                   grid_cars=args.grid_cars,
                                   program=args.scene_program or program_scene(_cfg),
                                   pre=args.scene_pre or f"{prefix}Starting Soon",
                                   post=args.scene_post or f"{prefix}Race Complete",
                                   post_nonrace=args.scene_nonrace or f"{prefix}Intermission"),
            host=args.obs_host, port=args.obs_port,
            password=args.obs_password or _cfg.obs.password or None,
            transition=args.scene_transition)

    favourites = frozenset(n.strip() for n in (args.favourites or "").split(",") if n.strip())
    cfg = director_config(replays=args.replays, min_shot=args.min_shot,
                          max_shot=args.max_shot, cut_margin=args.cut_margin,
                          favourites=favourites)
    if favourites:
        print(f"favourites: {', '.join('#' + n for n in sorted(favourites))}")
    replay = None
    if args.replays:
        replay = replay_director(roll=args.replay_roll, lead_in=args.replay_lead_in,
                                 slow_motion=args.replay_slow_mo)
    # The logger needs the actuator built for THIS connection (car numbers and the
    # track's camera groups both come from its session info), so it is rebuilt on
    # every reconnect along with the actuator itself.
    logger: dict = {}

    def make_actuator(info):
        actuator = actuator_for(info, args.angles)
        logger["on_decision"] = _log_decision(actuator)
        logger["on_camera"] = _log_camera(actuator)
        return actuator

    def on_connect(info) -> None:
        print(f"connected: track={info.track_name} | drivers={len(info.drivers())} "
              f"| angles={args.angles}"
              + (f" | replays ON (lead-in {args.replay_lead_in:.0f}s, cap {args.replay_roll:.0f}s)"
                 if replay else ""))

    def on_replay(phase: str, cand) -> None:
        if phase == "start" and cand is not None:
            print(f"[t={cand.at:8.1f}s]  REPLAY   {cand.kind} #{cand.car_number} "
                  f"{cand.driver} P{cand.position}"
                  + ("  (was on camera)" if cand.seen_on_camera else "  (missed live)"))
        elif phase == "end":
            print("            REPLAY   ...and back to the race")
        elif phase == "failed":
            who = f" ({cand.kind} #{cand.car_number} {cand.driver})" if cand is not None else ""
            print(f"            REPLAY   seek never landed; abandoned{who}")

    def on_disconnect(reason: str) -> None:
        print(f"\nbridge lost ({reason}); reconnecting to {args.url} ...")

    def on_decision(dec, cmd) -> None:
        fn = logger.get("on_decision")
        if fn is not None:
            fn(dec, cmd)

    def on_camera(drift, cmd) -> None:
        fn = logger.get("on_camera")
        if fn is not None:
            fn(drift, cmd)

    async def go() -> None:
        n = await drive_live_forever(
            args.url, cfg=cfg, actuator_factory=make_actuator, limit=args.limit,
            takeover=args.takeover, on_decision=on_decision, on_snapshot=on_snapshot,
            on_camera=on_camera, on_connect=on_connect,
            on_disconnect=on_disconnect, reconnect=args.reconnect,
            replay=replay, on_replay=on_replay)
        print(f"\nsent {n} camera commands")

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def cmd_replay_probe(args: argparse.Namespace) -> int:
    """Prove the sim honours a replay seek. Moves the real broadcast picture."""
    import asyncio

    from .camera.probe import probe_replay

    print(f"probing {args.url}: seek back {args.back:.0f}s, hold {args.hold:.0f}s"
          f"{', slow-mo' if args.slow_motion else ''}")
    print("this MOVES THE LIVE PICTURE. Ctrl-C leaves the sim in replay; if that happens,")
    print("scrub to the live edge in the sim (or re-run with --back 0) to recover.\n")

    def on_sample(s) -> None:
        flag = "?" if s.is_replay is None else ("REPLAY" if s.is_replay else "live")
        print(f"  +{s.wall:5.2f}s  session_time={s.session_time:9.1f}  {flag}")

    async def go():
        return await probe_replay(args.url, back=args.back, hold=args.hold,
                                  slow_motion=args.slow_motion, on_sample=on_sample)

    try:
        res = asyncio.run(go())
    except KeyboardInterrupt:
        print("\ninterrupted: the sim may still be in replay. Scrub it to live.")
        return 1
    print(res.report())
    return 0 if res.seek_seen_after is not None and res.returned_after is not None else 1


def cmd_broadcast(args: argparse.Namespace) -> int:
    import asyncio

    from .show.live import broadcast
    from .show.wiring import actuator_for, command_sink, telemetry_source

    try:
        src = telemetry_source(live=args.live, file=args.file, hz=args.hz)
    except RuntimeError as e:
        print(e)
        return 1
    if src is None:
        print("give a recording FILE to drive from, or use --live")
        return 2

    info = src.session_info()
    print(f"track: {info.track_name} | drivers: {len(info.drivers())} | angles: {args.angles}")
    actuator = actuator_for(info, args.angles)
    sink, where = command_sink(bridge=args.bridge, sdk=args.sdk)
    print(f"sink: {where}")

    cfg = DirectorConfig(min_shot=args.min_shot, max_shot=args.max_shot, cut_margin=args.cut_margin)

    async def go() -> int:
        try:
            n = await broadcast(src, sink, cfg=cfg, actuator=actuator, limit=args.limit,
                                takeover=args.takeover, on_decision=_log_decision(actuator))
        finally:
            await sink.aclose()
        print(f"\nsent {n} camera commands")
        return n

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pylon",
        description="Automatic iRacing broadcast direction: it watches the race over "
                    "telemetry, points the sim's own cameras at what matters, and runs "
                    "the timing tower and the scene switcher in OBS.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synth", help="generate a synthetic race recording")
    s.add_argument("out")
    s.add_argument("--cars", type=int, default=20)
    s.add_argument("--seconds", type=float, default=180.0)
    s.add_argument("--hz", type=float, default=10.0)
    s.add_argument("--seed", type=int, default=1234)
    s.set_defaults(func=cmd_synth)

    d = sub.add_parser("describe", help="summarise a recording or .ibt file")
    d.add_argument("file")
    d.set_defaults(func=cmd_describe)

    r = sub.add_parser("replay", help="print a running-order preview of a recording")
    r.add_argument("file")
    r.add_argument("--top", type=int, default=8)
    r.add_argument("--every", type=float, default=2.0, help="seconds between printouts")
    r.add_argument("--limit", type=float, default=0.0, help="stop after this many session seconds")
    r.set_defaults(func=cmd_replay)

    st = sub.add_parser("story", help="print the world-model story (battles, incidents, events)")
    st.add_argument("file")
    st.add_argument("--battles", type=int, default=3)
    st.add_argument("--every", type=float, default=5.0, help="seconds between periodic printouts")
    st.add_argument("--limit", type=float, default=0.0, help="stop after this many session seconds")
    st.set_defaults(func=cmd_story)

    dr = sub.add_parser("direct", help="dry-run the director; print the shot list + summary")
    dr.add_argument("file")
    dr.add_argument("--limit", type=float, default=0.0, help="stop after this many session seconds")
    dr.add_argument("--min-shot", type=float, default=DirectorConfig.min_shot, dest="min_shot")
    dr.add_argument("--max-shot", type=float, default=DirectorConfig.max_shot, dest="max_shot")
    dr.add_argument("--cut-margin", type=float, default=DirectorConfig.cut_margin, dest="cut_margin")
    dr.set_defaults(func=cmd_direct)

    ov = sub.add_parser("overlay", help="serve the overlay from a recording or a live bridge")
    ov.add_argument("file", nargs="?", help="recording to replay (omit with --bridge)")
    ov.add_argument("--bridge", default=None, metavar="URL",
                    help="feed live telemetry from a bridge, e.g. ws://simbox:8779")
    ov.add_argument("--host", default=SHOW.bind_host)
    ov.add_argument("--ws-port", type=int, default=SHOW.overlay_ws_port, dest="ws_port")
    ov.add_argument("--http-port", type=int, default=SHOW.overlay_http_port, dest="http_port")
    ov.add_argument("--rate", type=float, default=SHOW.overlay_rate,
                    help="tower ticks per second when replaying a recording")
    ov.add_argument("--once", action="store_true", help="play once instead of looping")
    ov.set_defaults(func=cmd_overlay)

    cf = sub.add_parser("config", help="show the settings file, or open it for editing")
    cf.add_argument("--edit", action="store_true", help="open it in your text editor")
    cf.add_argument("--path", action="store_true", help="print only where it is")
    cf.set_defaults(func=cmd_config)

    op = sub.add_parser("obs-prepare",
                        help="switch on obs-websocket and add the control panel "
                             "(OBS must be CLOSED)")
    op.add_argument("--panel-url", dest="panel_url", default=None,
                    help="the dock's URL (default: this install's control panel)")
    op.add_argument("--panel-title", dest="panel_title", default="Pylon",
                    help="what the dock is called in OBS")
    op.add_argument("--force", action="store_true",
                    help="edit even with OBS running. It will undo the change when "
                         "it closes, so this is for testing only")
    op.set_defaults(func=cmd_obs_prepare)

    dc = sub.add_parser("doctor", help="check OBS, iRacing, the scenes and the ports")
    dc.add_argument("--no-sim", action="store_true",
                    help="skip the iRacing check (it opens the sim's shared memory)")
    dc.set_defaults(func=cmd_doctor)

    stu = sub.add_parser("studio", help="run the whole show, supervised (the one launcher)")
    stu.add_argument("--bridge-port", dest="bridge_port", type=int, default=SHOW.bridge_port)
    stu.add_argument("--replay", default=None, metavar="FILE",
                     help="feed the bridge a recording instead of the live sim, so the "
                          "whole studio can be exercised with no session running")
    stu.add_argument("--no-replays", action="store_true", dest="no_replays",
                     help="do not roll instant replays after a crash (the config file's "
                          "director.replays is what normally decides)")
    stu.add_argument("--no-obs-scenes", action="store_true", dest="no_obs_scenes",
                     help="do not let the director switch OBS scenes")
    stu.add_argument("--log-dir", dest="log_dir", default=None, metavar="DIR",
                     help="where to tee worker stdout/stderr (default: per-user state "
                          "dir). Without this a crashed worker leaves no trace at all")
    stu.add_argument("--no-worker-logs", action="store_true", dest="no_worker_logs",
                     help="do not write worker logs")
    stu.add_argument("--interval", type=float, default=2.0,
                     help="seconds between supervision passes")
    stu.add_argument("--control-port", dest="control_port", type=int,
                     default=SHOW.control_port,
                     help="port for the control panel (added to OBS as a Custom "
                          "Browser Dock)")
    stu.add_argument("--control-host", dest="control_host", default=SHOW.loopback,
                     help="loopback on purpose: the panel can restart workers and end "
                          "the broadcast, and carries no authentication")
    stu.add_argument("--no-control", action="store_true", dest="no_control",
                     help="do not serve the control panel")
    stu.set_defaults(func=cmd_studio)

    rec = sub.add_parser("record", help="capture live telemetry (Win32 only)")
    rec.add_argument("out")
    rec.add_argument("--hz", type=float, default=SHOW.live_hz)
    rec.add_argument("--duration", type=float, default=None)
    rec.set_defaults(func=cmd_record)

    br = sub.add_parser("bridge", help="run the LAN bridge (telemetry out + camera commands in)")
    br.add_argument("--host", default=SHOW.bind_host)
    br.add_argument("--port", type=int, default=SHOW.bridge_port)
    br.add_argument("--sdk", action="store_true",
                    help="drive iRacing via pyirsdk (run on the Win32 sim box)")
    br.add_argument("--live", action="store_true",
                    help="stream live telemetry from iRacing (Win32 sim box)")
    br.add_argument("--replay", metavar="FILE",
                    help="stream a recording as the telemetry source (Linux dev)")
    br.add_argument("--hz", type=float, default=SHOW.live_hz,
                    help="live poll rate (with --live)")
    br.add_argument("--rate", type=float, default=None,
                    help="frames/sec pacing when streaming a --replay (default: as fast as possible)")
    br.add_argument("--record", metavar="OUT.jsonl.gz", default=None,
                    help="also write the streamed telemetry to a recording (offline tuning)")
    br.set_defaults(func=cmd_bridge)

    os_ = sub.add_parser("obs-setup",
                         help="provision the OBS broadcast scene (sim capture + overlay)")
    os_.add_argument("--host", default=SHOW.obs_host)
    os_.add_argument("--port", type=int, default=SHOW.obs_port)
    os_.add_argument("--password", default=None, help="obs-websocket password (default: read from OBS config)")
    # The same constant the director's switcher defaults to, so the scene
    # obs-setup builds is the one `live --obs-scenes` cuts back to. They were
    # two literals once, and the director spent a show unable to leave the
    # holding card because OBS had "Program" and it wanted the other name.
    os_.add_argument("--scene", default=None,
                     help="the programme scene to build (default: named from the show "
                          "tag in config.toml, which is what keeps two broadcasters on "
                          "one PC apart)")
    os_.add_argument("--game-window", default=None, dest="game_window",
                     help="pin game capture to a window, as title:class:exe "
                          "(default: whatever is fullscreen)")
    os_.add_argument("--capture-crop", dest="capture_crop", default="center",
                     help="crop a local capture to 'center' (a canvas-sized window out of "
                          "the middle, which is what a triple-wide sim wants), 'none', "
                          "or an explicit WxH. NDI is never cropped.")
    os_.add_argument("--overlay-url", dest="overlay_url",
                     default=SHOW.overlay_page_url())
    os_.add_argument("--cards-url", dest="cards_url", default=SHOW.cards_url())
    os_.add_argument("--scene-prefix", dest="scene_prefix", default=None,
                     help="prefix for the holding-scene names, so OBS groups them "
                          "together in its (alphabetical) scene list")
    os_.add_argument("--no-cards", action="store_true", dest="no_cards",
                     help="skip the Starting Soon / Intermission / Race Complete scenes")
    os_.add_argument("--width", type=int, default=SHOW.canvas[0])
    os_.add_argument("--height", type=int, default=SHOW.canvas[1])
    os_.add_argument("--fps", type=int, default=SHOW.fps)
    os_.add_argument("--bitrate", type=int, default=OutputSettings.bitrate,
                     help="stream video bitrate, kbps CBR (default: %(default)s)")
    os_.add_argument("--keyint", type=int, default=OutputSettings.keyint_sec,
                     help="keyframe interval in seconds; YouTube's spec is 2, never above 4 "
                          "(default: %(default)s)")
    os_.add_argument("--nvenc-preset", dest="nvenc_preset", default=OutputSettings.preset,
                     help="NVENC preset p1 (fastest) to p7 (best) (default: %(default)s)")
    os_.add_argument("--no-output", action="store_true", dest="no_output",
                     help="leave OBS's output mode and encoder settings as they are")
    os_.set_defaults(func=cmd_obs_setup)

    lv = sub.add_parser("live", help="connect to a streaming bridge and direct it (Linux brain)")
    lv.add_argument("url", help="bridge WebSocket, e.g. ws://simbox:8779")
    lv.add_argument("--takeover", action="store_true",
                    help="hide the UI and take manual camera control at start")
    lv.add_argument("--limit", type=float, default=0.0, help="stop after this many session seconds")
    lv.add_argument("--favourites", default=",".join(_cfg.director.favourites), metavar="NUMBERS",
                    help="car numbers to give extra screen time, comma-separated "
                         "(default: whatever the settings file says)")
    lv.add_argument("--min-shot", type=float, default=_cfg.director.min_shot, dest="min_shot")
    lv.add_argument("--max-shot", type=float, default=DirectorConfig.max_shot, dest="max_shot")
    lv.add_argument("--cut-margin", type=float, default=DirectorConfig.cut_margin, dest="cut_margin")
    lv.add_argument("--angles", choices=ANGLE_CHOICES, default=_angles_default(),
                    help="camera-angle personality (default: classic)")
    lv.add_argument("--replays", action="store_true",
                    help="enable instant replays: seek the sim's own tape on a big "
                         "moment, roll it, and come back (off by default here; the "
                         "studio takes this from the config file instead)")
    lv.add_argument("--replay-roll", type=float, default=ReplayConfig.roll, dest="replay_roll",
                    help="wall-clock cap on one replay excursion; its length is set by "
                         "the tape (lead-in, slow motion, outcome), this only ends one "
                         "whose tape stopped advancing (default: %(default)s)")
    lv.add_argument("--replay-lead-in", type=float, default=ReplayConfig.lead_in,
                    dest="replay_lead_in",
                    help="seconds of tape shown BEFORE the marked moment (default: %(default)s)")
    lv.add_argument("--replay-slow-mo", action="store_true", dest="replay_slow_mo",
                    help="drop to slow motion through the marked moment. OFF by default: "
                         "the sim plays no audio in slow motion, and at the measured "
                         "rate the moment ran silent for 7s on air")
    lv.add_argument("--reconnect", type=float, default=SHOW.reconnect,
                    help="seconds to wait before retrying a dropped bridge (default: %(default)s)")
    lv.add_argument("--obs-scenes", action="store_true", dest="obs_scenes",
                    help="also switch OBS scenes from the session state: a holding "
                         "card before the session, the programme once it is running, "
                         "a closing card after the chequer (OFF by default: this "
                         "takes over your switcher)")
    lv.add_argument("--obs-host", default=SHOW.obs_host, dest="obs_host")
    lv.add_argument("--obs-port", type=int, default=SHOW.obs_port, dest="obs_port")
    lv.add_argument("--obs-password", default=None, dest="obs_password",
                    help="obs-websocket password (default: read from OBS config)")
    # Scene names default to what `pylon obs-setup` builds; override any of
    # them if OBS calls them something else.
    lv.add_argument("--scene-program", default=None, dest="scene_program")
    lv.add_argument("--scene-pre", default=None, dest="scene_pre")
    lv.add_argument("--scene-post", default=None, dest="scene_post")
    lv.add_argument("--scene-nonrace", default=None, dest="scene_nonrace",
                    help="closing card for practice/qualifying")
    lv.add_argument("--scene-transition", default=SHOW.scene_transition, dest="scene_transition",
                    help="OBS transition to select before each scene cut. Create it in "
                         "OBS first; obs-websocket cannot make one (default: %(default)s)")
    lv.add_argument("--post-race-hold", type=float, default=SceneConfig.post_race_hold,
                    dest="post_race_hold",
                    help="stay on the programme through the whole chequer (the field is "
                         "still finishing) and this long into the cool-down, then cut to "
                         "the closing card (default: %(default)s)")
    lv.add_argument("--chequer-max-hold", type=float, default=SceneConfig.chequer_max_hold,
                    dest="chequer_max_hold",
                    help="ceiling: the card comes this long after the flag even if the "
                         "sim never reaches cool-down (default: %(default)s)")
    lv.add_argument("--grid-cars", type=int, default=SceneConfig.grid_cars, dest="grid_cars",
                    help="in a race session, cut from the Starting Soon card to the "
                         "programme once this many cars are on the grid; drivers grid "
                         "themselves over the countdown (default: %(default)s)")
    lv.set_defaults(func=cmd_live)

    rp = sub.add_parser("replay-probe",
                        help="prove the sim honours a replay seek (MOVES THE LIVE PICTURE)")
    rp.add_argument("url", help="bridge WebSocket, e.g. ws://simbox:8779")
    rp.add_argument("--back", type=float, default=30.0,
                    help="seconds to seek back from now (default: 30)")
    rp.add_argument("--hold", type=float, default=8.0,
                    help="seconds to stay in the replay before returning (default: 8)")
    rp.add_argument("--slow-motion", action="store_true", dest="slow_motion",
                    help="also test native slow motion while there")
    rp.set_defaults(func=cmd_replay_probe)

    bc = sub.add_parser("broadcast", help="run the director and drive cameras from a local source")
    bc.add_argument("file", nargs="?", help="recording to drive from (omit with --live)")
    bc.add_argument("--live", action="store_true", help="read live telemetry (Win32 sim box)")
    bc.add_argument("--hz", type=float, default=SHOW.live_hz, help="live poll rate")
    bc.add_argument("--bridge", default=None, metavar="URL",
                    help="send commands to a remote bridge, e.g. ws://simbox:8779")
    bc.add_argument("--sdk", action="store_true",
                    help="drive a local iRacing via pyirsdk (director co-located with the sim)")
    bc.add_argument("--takeover", action="store_true",
                    help="hide the UI and take manual camera control at start")
    bc.add_argument("--limit", type=float, default=0.0, help="stop after this many session seconds")
    bc.add_argument("--min-shot", type=float, default=DirectorConfig.min_shot, dest="min_shot")
    bc.add_argument("--max-shot", type=float, default=DirectorConfig.max_shot, dest="max_shot")
    bc.add_argument("--cut-margin", type=float, default=DirectorConfig.cut_margin, dest="cut_margin")
    bc.add_argument("--angles", choices=ANGLE_CHOICES, default="classic",
                    help="camera-angle personality (default: classic)")
    bc.set_defaults(func=cmd_broadcast)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
