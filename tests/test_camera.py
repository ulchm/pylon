import asyncio
import json

import pytest
import websockets

from pylon.camera import (
    Actuator,
    ActuatorConfig,
    Angle,
    AnglePolicy,
    AngleRotator,
    BridgeClient,
    BridgeServer,
    CamCommand,
    CameraMap,
    CameraState,
    CamOp,
    CsMode,
    DirectSink,
    RecordingController,
    RpyPosMode,
    RpySrchMode,
    WsCommandClient,
    apply,
)
from pylon.camera.sdk import SdkCameraController
from pylon.director import DirectorConfig
from pylon.director import run as run_director
from pylon.director.model import Shot, ShotFlavor, ShotKind
from pylon.show.live import broadcast, drive_live, drive_live_forever
from pylon.telemetry import SyntheticSource
from pylon.telemetry.frame import SessionInfo


def _session_with_cams() -> SessionInfo:
    """A session with real TV group numbers (as captured at Spa) and a roster."""
    return SessionInfo({
        "DriverInfo": {"Drivers": [
            {"CarIdx": 0, "CarNumber": "1"},
            {"CarIdx": 1, "CarNumber": "44"},
            {"CarIdx": 2, "CarNumber": "7"},
            {"CarIdx": 64, "CarNumber": "0", "CarIsPaceCar": 1},
        ]},
        "CameraInfo": {"Groups": [
            {"GroupNum": 1, "GroupName": "Nose"},
            {"GroupNum": 9, "GroupName": "Cockpit"},
            {"GroupNum": 11, "GroupName": "TV1"},
            {"GroupNum": 12, "GroupName": "TV2"},
            {"GroupNum": 13, "GroupName": "TV3"},
            {"GroupNum": 20, "GroupName": "Chase"},
        ]},
    })


# a policy that pins every shot to TV1, for tests that care about targets not angles
_TV1_ONLY = AnglePolicy(profiles={}, default=(Angle.TV1,))


# --- command protocol ------------------------------------------------------
def test_command_roundtrip():
    for cmd in (
        CamCommand.switch_num("44", 11, 0, label="lead", session_time=12.5),
        CamCommand.switch_pos(CsMode.AT_INCIDENT, 12, 1),
        CamCommand.set_state(CameraState.UI_HIDDEN),
    ):
        assert CamCommand.from_dict(cmd.to_dict()) == cmd


def test_switch_num_coerces_car_number_to_string():
    c = CamCommand.switch_num(44, 11)
    assert c.car_number == "44"
    assert c.to_dict()["camera"] == 0


def test_set_state_wire_omits_camera():
    assert "camera" not in CamCommand.set_state(CameraState.UI_HIDDEN).to_dict()


# --- replay verbs on the command protocol ----------------------------------
def test_replay_command_roundtrip():
    for cmd in (
        CamCommand.replay_seek(2, 1234.5, label="wreck"),
        CamCommand.replay_search(RpySrchMode.TO_END),
        CamCommand.replay_speed(2, slow_motion=True),
        CamCommand.replay_speed(0),                    # 0 = pause, and must survive
        CamCommand.replay_pos(RpyPosMode.END, -300),
    ):
        assert CamCommand.from_dict(cmd.to_dict()) == cmd


def test_replay_seek_converts_seconds_to_milliseconds():
    """The director marks a session time in seconds; the SDK wants milliseconds. The
    conversion lives in the constructor so no caller has to remember which unit it holds."""
    c = CamCommand.replay_seek(1, 1800.6)
    assert c.session_time_ms == 1800600
    assert c.session_num == 1
    # `session_time` (when we decided) is a DIFFERENT field from the instant we seek to.
    assert c.session_time is None


def test_replay_wire_omits_camera_and_unset_fields():
    d = CamCommand.replay_search(RpySrchMode.PREV_INCIDENT).to_dict()
    assert "camera" not in d and "speed" not in d and "session_time_ms" not in d
    assert d == {"op": CamOp.REPLAY_SEARCH, "search_mode": RpySrchMode.PREV_INCIDENT}


# --- camera map ------------------------------------------------------------
def test_camera_map_resolves_tv_groups():
    cmap = CameraMap.from_session(_session_with_cams())
    assert cmap.resolve("TV1") == 11
    assert cmap.resolve("TV2") == 12
    assert cmap.resolve("TV3") == 13
    assert cmap.resolve("does-not-exist") == 11  # defaults to TV1


def test_camera_map_fallback_without_camera_info():
    cmap = CameraMap.from_session(SessionInfo({"DriverInfo": {"Drivers": []}}))
    assert cmap.resolve("TV1") == 1  # DEFAULT_GROUP


# --- actuator --------------------------------------------------------------
def test_actuator_maps_shot_kinds_to_car_numbers():
    # pin angles to TV1 so this test is only about *who* we frame, not the angle
    act = Actuator(_session_with_cams(), ActuatorConfig(policy=_TV1_ONLY))

    battle = Shot(ShotKind.BATTLE, "battle:0:1", 1, "P1 #1 vs P2 #44 0.40s", (0, 1))
    cmd = act.command_for(battle, session_time=12.3)
    assert cmd.op == CamOp.SWITCH_NUM
    assert cmd.car_number == "44"        # the attacker
    assert cmd.group == 11               # TV1
    assert cmd.session_time == 12.3

    assert act.command_for(Shot(ShotKind.LEADER, "leader", 0, "#1")).car_number == "1"
    assert act.command_for(Shot(ShotKind.INCIDENT, "incident:2:5", 2, "#7")).car_number == "7"


def test_actuator_rotates_the_ranked_angles_the_track_has_and_never_the_cockpit():
    """The classic policy rotates a leader shot through its ranked angles, skipping
    the ones this track lacks, so the same look isn't reused back-to-back. Groups
    here: Nose=1, Cockpit=9, TV1=11, TV3=13, Chase=20 (no TV Mixed / Static / Far
    Chase / Blimp). The cockpit is never among them (2026-09-21: it shows the
    mirrors, and the operator does not want that on air)."""
    act = Actuator(_session_with_cams())  # default = classic
    solo = Shot(ShotKind.LEADER, "leader", 0, "#1", flavor=ShotFlavor.SOLO)
    groups = [act.command_for(solo).group for _ in range(7)]
    assert groups == [11, 1, 13, 11, 1, 13, 11]   # TV1, Nose, TV3, round again
    assert 9 not in groups
    follow = Shot(ShotKind.FOLLOW, "follow", 0, "#1")
    assert 9 not in {act.command_for(follow).group for _ in range(12)}


def test_a_pit_stop_is_shot_from_the_lane_and_the_rejoin_from_the_road():
    """The two lane groups alternate between stops; the way out goes to a camera that
    can see the road the car is merging into."""
    info = SessionInfo({
        "DriverInfo": {"Drivers": [{"CarIdx": 0, "CarNumber": "1"}]},
        "CameraInfo": {"Groups": [
            {"GroupNum": 11, "GroupName": "TV1"}, {"GroupNum": 16, "GroupName": "Pit Lane"},
            {"GroupNum": 17, "GroupName": "Pit Lane 2"}, {"GroupNum": 20, "GroupName": "Chase"},
        ]},
    })
    act = Actuator(info)
    stop = Shot(ShotKind.PIT, "pit:0", 0, "#1 pits")
    assert [act.command_for(stop).group for _ in range(3)] == [16, 17, 20]
    out = Shot(ShotKind.FOLLOW, "follow:0", 0, "#1 rejoins", flavor=ShotFlavor.REJOIN)
    assert act.command_for(out).group == 11          # Chase was just used: not back-to-back
    assert act.command_for(out).group == 20


def test_a_track_without_lane_cameras_still_frames_a_stop():
    act = Actuator(_session_with_cams())
    stop = Shot(ShotKind.PIT, "pit:0", 0, "#1 pits")
    assert act.command_for(stop).group in (20, 11)   # Chase / TV1, the fallbacks in rank


def test_actuator_side_by_side_battle_gets_chase_angle():
    """A wheel-to-wheel battle is framed differently than a car merely closing in:
    classic sends side-by-side to Chase first, a gap battle to a trackside TV."""
    sbs = Shot(ShotKind.BATTLE, "b", 1, "", (0, 1), flavor=ShotFlavor.SIDE_BY_SIDE)
    assert Actuator(_session_with_cams()).command_for(sbs).group == 20  # Chase

    gap = Shot(ShotKind.BATTLE, "b", 1, "", (0, 1))  # no flavor -> just within range
    assert Actuator(_session_with_cams()).command_for(gap).group == 12  # TV2


def test_actuator_falls_back_when_track_lacks_preferred_angles():
    """A track that has none of a shot's preferred angles still cuts somewhere sane:
    the CameraMap's default group (here TV1, the only broadcast group present)."""
    thin = SessionInfo({
        "DriverInfo": {"Drivers": [{"CarIdx": 0, "CarNumber": "1"}]},
        "CameraInfo": {"Groups": [{"GroupNum": 7, "GroupName": "TV1"}]},
    })
    act = Actuator(thin)
    assert act.command_for(Shot(ShotKind.LEADER, "l", 0, "#1")).group == 7


def test_angle_rotator_avoids_identical_back_to_back_group():
    cmap = CameraMap({"TV1": 11, "TV2": 12})
    policy = AnglePolicy({(ShotKind.LEADER, ""): (Angle.TV1,),
                          (ShotKind.BATTLE, ""): (Angle.TV1, Angle.TV2)})
    rot = AngleRotator(policy)
    assert rot.pick(cmap, ShotKind.LEADER, "") == 11
    # battle's top pick is also TV1 (11) but that would repeat -> skip to TV2 (12)
    assert rot.pick(cmap, ShotKind.BATTLE, "") == 12


def test_actuator_falls_back_to_special_targets():
    act = Actuator(_session_with_cams())
    # idx 99 has no driver: use a position-based special target instead of a number
    inc = act.command_for(Shot(ShotKind.INCIDENT, "incident:99:1", 99, "?"))
    assert inc.op == CamOp.SWITCH_POS and inc.position == CsMode.AT_INCIDENT
    lead = act.command_for(Shot(ShotKind.LEADER, "leader", 99, "?"))
    assert lead.op == CamOp.SWITCH_POS and lead.position == CsMode.AT_LEADER
    # an unframeable battle yields no command
    assert act.command_for(Shot(ShotKind.BATTLE, "b", 99, "?")) is None


# --- controller dispatch ---------------------------------------------------
def test_apply_dispatches_each_op():
    rc = RecordingController()
    apply(rc, CamCommand.switch_num("44", 11))
    apply(rc, CamCommand.switch_pos(CsMode.AT_LEADER, 12, 1))
    apply(rc, CamCommand.set_state(CameraState.UI_HIDDEN))
    assert [c.op for c in rc.calls] == [CamOp.SWITCH_NUM, CamOp.SWITCH_POS, CamOp.SET_STATE]
    assert rc.calls[0].car_number == "44" and rc.calls[0].group == 11
    assert rc.calls[1].position == CsMode.AT_LEADER and rc.calls[1].camera == 1
    assert rc.calls[2].state == CameraState.UI_HIDDEN


def test_apply_rejects_unknown_op():
    with pytest.raises(ValueError):
        apply(RecordingController(), CamCommand(op="bogus"))


def test_sdk_controller_delegates_to_irsdk():
    """The Win32 wrapper's delegation, exercised with a fake ir (no SDK needed)."""
    class FakeIR:
        is_connected = True

        def __init__(self):
            self.calls = []

        def cam_switch_num(self, num, group, camera):
            self.calls.append(("num", num, group, camera))

        def cam_switch_pos(self, pos, group, camera):
            self.calls.append(("pos", pos, group, camera))

        def cam_set_state(self, state):
            self.calls.append(("state", state))

    ir = FakeIR()
    ctrl = SdkCameraController(ir=ir)
    apply(ctrl, CamCommand.switch_num("44", 11, 2))
    apply(ctrl, CamCommand.switch_pos(CsMode.AT_INCIDENT, 12))
    apply(ctrl, CamCommand.set_state(CameraState.UI_HIDDEN))
    assert ir.calls == [
        ("num", "44", 11, 2),
        ("pos", -3, 12, 0),
        ("state", CameraState.UI_HIDDEN),
    ]
    assert ctrl.connected is True


def test_apply_dispatches_replay_ops():
    rc = RecordingController()
    apply(rc, CamCommand.replay_seek(2, 1800.6))
    apply(rc, CamCommand.replay_search(RpySrchMode.TO_END))
    apply(rc, CamCommand.replay_speed(2, slow_motion=True))
    apply(rc, CamCommand.replay_pos(RpyPosMode.END, -300))
    assert [c.op for c in rc.calls] == [
        CamOp.REPLAY_SEEK, CamOp.REPLAY_SEARCH, CamOp.REPLAY_SPEED, CamOp.REPLAY_POS]
    assert rc.calls[0].session_num == 2 and rc.calls[0].session_time_ms == 1800600
    assert rc.calls[1].search_mode == RpySrchMode.TO_END
    assert rc.calls[2].speed == 2 and rc.calls[2].slow_motion is True
    assert rc.calls[3].pos_mode == RpyPosMode.END and rc.calls[3].frame_num == -300


def test_sdk_controller_delegates_replay_verbs_to_irsdk():
    """The Win32 replay wrapper, exercised with a fake ir. Argument passing is the whole
    point of the test: a seek to the wrong unit lands 30 minutes away, and slow_motion
    silently defaulting to False is the difference between a produced replay and a blur."""
    class FakeIR:
        is_connected = True

        def __init__(self):
            self.calls = []

        def replay_search_session_time(self, session_num, session_time_ms):
            self.calls.append(("seek", session_num, session_time_ms))

        def replay_search(self, mode):
            self.calls.append(("search", mode))

        def replay_set_play_speed(self, speed, slow_motion):
            self.calls.append(("speed", speed, slow_motion))

        def replay_set_play_position(self, pos_mode, frame_num):
            self.calls.append(("pos", pos_mode, frame_num))

    ir = FakeIR()
    ctrl = SdkCameraController(ir=ir)
    apply(ctrl, CamCommand.replay_seek(2, 1800.6))
    apply(ctrl, CamCommand.replay_search(RpySrchMode.TO_END))
    apply(ctrl, CamCommand.replay_speed(2, slow_motion=True))
    apply(ctrl, CamCommand.replay_pos(RpyPosMode.END, 0))
    assert ir.calls == [
        ("seek", 2, 1800600),
        ("search", RpySrchMode.TO_END),
        ("speed", 2, True),
        ("pos", RpyPosMode.END, 0),
    ]


def test_bridge_ignores_an_op_the_agent_is_too_old_to_know():
    """The sim-box agent is deployed by hand and routinely lags the Linux brain, so a
    forward-dated op WILL arrive. It must be a logged no-op: letting it escape the
    handler closes the socket and takes the whole telemetry stream down with it."""
    async def go():
        import json

        import websockets

        rc = RecordingController()
        server = BridgeServer(rc, host="127.0.0.1", port=8794)
        async with server.ws_server(), websockets.connect("ws://127.0.0.1:8794") as ws:
            await ws.send(json.dumps({"op": "replay_teleport_to_2027"}))
            await ws.send(json.dumps(CamCommand.switch_num("7", 13).to_dict()))
            for _ in range(200):
                if rc.calls:
                    break
                await asyncio.sleep(0.01)
            still_open = ws.state.name == "OPEN"
        return rc.calls, still_open

    calls, still_open = asyncio.run(go())
    assert still_open, "an unknown op must not drop the connection"
    assert [c.op for c in calls] == [CamOp.SWITCH_NUM]


# --- end to end: director -> actuator -> controller ------------------------
def _race():
    return SyntheticSource(num_cars=16, duration_s=120.0, hz=10.0, seed=7, incident_at=60.0,
                           incident_cars=2)


def test_broadcast_emits_one_command_per_cut_matching_targets():
    """Cut-to-battle proven end-to-end on Linux: every director decision becomes a
    camera command aimed at that shot's car. Only the final Win32 send is stubbed."""
    cfg = DirectorConfig()
    decisions = list(run_director(_race(), cfg))          # ground truth
    numbers = _race().session_info().drivers_by_idx()

    rc = RecordingController()
    n = asyncio.run(broadcast(_race(), DirectSink(rc), cfg=cfg))

    assert n == len(decisions)
    assert len(rc.calls) == len(decisions)
    for dec, cmd in zip(decisions, rc.calls):
        assert cmd.op == CamOp.SWITCH_NUM
        assert cmd.car_number == numbers[dec.shot.target_idx].number
        assert cmd.group == 1  # synthetic has no CameraInfo -> DEFAULT_GROUP
    assert any(d.shot.kind == ShotKind.INCIDENT for d in decisions)  # incident was covered


def test_broadcast_takeover_sends_state_first():
    rc = RecordingController()
    asyncio.run(broadcast(_race(), DirectSink(rc), cfg=DirectorConfig(),
                          limit=20.0, takeover=True))
    assert rc.calls[0].op == CamOp.SET_STATE
    assert rc.calls[0].state == CameraState.UI_HIDDEN


# --- transport: the WebSocket bridge ---------------------------------------
def test_ws_bridge_roundtrips_commands():
    async def go():
        rc = RecordingController()
        server = BridgeServer(rc, host="127.0.0.1", port=8791)
        async with server.ws_server():
            client = WsCommandClient("ws://127.0.0.1:8791")
            await client.send(CamCommand.switch_num("44", 11, label="lead"))
            await client.send(CamCommand.set_state(CameraState.UI_HIDDEN))
            await client.aclose()
            for _ in range(200):  # let the server apply both frames
                if len(rc.calls) >= 2:
                    break
                await asyncio.sleep(0.01)
        return rc.calls

    calls = asyncio.run(go())
    assert [c.op for c in calls] == [CamOp.SWITCH_NUM, CamOp.SET_STATE]
    assert calls[0].car_number == "44" and calls[0].group == 11


def test_bridge_ignores_malformed_frames():
    async def go():
        import json

        import websockets

        rc = RecordingController()
        server = BridgeServer(rc, host="127.0.0.1", port=8792)
        async with server.ws_server(), websockets.connect("ws://127.0.0.1:8792") as ws:
            await ws.send("not json at all")
            await ws.send('{"missing":"op"}')
            await ws.send(json.dumps(CamCommand.switch_num("7", 13).to_dict()))
            for _ in range(200):
                if rc.calls:
                    break
                await asyncio.sleep(0.01)
        return rc.calls

    calls = asyncio.run(go())
    # only the one valid command got through; the two malformed frames were ignored
    assert [c.op for c in calls] == [CamOp.SWITCH_NUM]
    assert calls[0].car_number == "7" and calls[0].group == 13


# --- bidirectional bridge: telemetry out + commands in ---------------------
def test_bridge_streams_session_and_frames_in_order():
    def make():
        return SyntheticSource(num_cars=8, duration_s=15.0, hz=10.0, seed=5)

    async def go():
        server = BridgeServer(source=make(), host="127.0.0.1", port=8793)
        got = []
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8793")
            info = await client.connect()
            async for fr in client.frames():
                got.append(fr)
            await client.aclose()
        return info, got

    info, got = asyncio.run(go())
    expected = list(make().frames())
    assert info.track_name == "Test Circuit"
    assert len(got) == len(expected)
    assert [f.tick for f in got] == [f.tick for f in expected]
    # frame payloads survive the JSON round-trip losslessly
    assert got[0].session_time == expected[0].session_time
    assert got[10].values["CarIdxLapDistPct"] == expected[10].values["CarIdxLapDistPct"]


def test_replay_probe_sequence_and_its_no_effect_verdict():
    """The probe against a source that ignores seeks, which is exactly what an old
    sim-box agent, or a sim that does not honour the broadcast message, looks like.

    Two things are pinned. The command SEQUENCE (seek, then speed back to real time
    BEFORE the search home, so a slow-mo probe does not crawl back), and the verdict:
    when the tape never moves the probe must say so plainly rather than report success
    off a clean return. A probe that cannot fail is not evidence.
    """
    async def go():
        from pylon.camera.probe import probe_replay

        rc = RecordingController()
        server = BridgeServer(rc, source=SyntheticSource(num_cars=6, duration_s=60.0,
                                                         hz=20.0, seed=3),
                              host="127.0.0.1", port=8795)
        async with server.ws_server():
            res = await probe_replay("ws://127.0.0.1:8795", back=30.0, hold=0.4,
                                     slow_motion=True)
        return rc.calls, res

    calls, res = asyncio.run(go())
    assert [c.op for c in calls] == [
        CamOp.REPLAY_SEEK, CamOp.REPLAY_SPEED, CamOp.REPLAY_SPEED, CamOp.REPLAY_SEEK]
    assert calls[1].slow_motion is True and calls[2].slow_motion is False
    # a synthetic source's clock only ever goes forward: no seek was honoured
    assert res.seek_seen_after is None
    assert "NO EFFECT" in res.report()
    # ...and with no IsReplayPlaying to read, coming home must take the SAFE route
    assert res.playing_tape is None
    assert calls[3].session_time_ms == round(res.mark * 1000)


def test_coming_home_from_a_replay_depends_on_where_we_started():
    """Measured live on 2026-07-25: the sim box was playing a saved 6.8h endurance
    tape, 1.3h in. `to_end` there means the FINISH, four and a half hours ahead, not
    the live edge every replay issue assumes. So the return verb is chosen from the
    pre-seek IsReplayPlaying, and anything short of positive evidence of a live edge
    takes the recoverable route."""
    from pylon.camera.probe import ProbeResult, home_commands

    live = ProbeResult(mark=1800.0, session_num=2, playing_tape=False)
    assert [c.op for c in home_commands(live)] == [CamOp.REPLAY_SEARCH]
    assert home_commands(live)[0].search_mode == RpySrchMode.TO_END

    tape = ProbeResult(mark=4742.0, session_num=2, playing_tape=True)
    cmds = home_commands(tape)
    assert [c.op for c in cmds] == [CamOp.REPLAY_SEEK]
    assert cmds[0].session_time_ms == 4742000
    assert "THE FINISH" in tape.returned_by

    unknown = ProbeResult(mark=100.0, session_num=0, playing_tape=None)
    assert [c.op for c in home_commands(unknown)] == [CamOp.REPLAY_SEEK]


def test_director_over_bridge_matches_local_direction():
    """The headline live path: telemetry streamed sim->Linux, director runs on the
    'Linux' end, camera cuts shipped back, and the sim-side controller receives the
    same cuts a local `direct` run produces. Whole loop, no threads, no sim box."""
    cfg = DirectorConfig()
    decisions = list(run_director(_race(), cfg))          # ground truth
    numbers = _race().session_info().drivers_by_idx()

    async def go():
        rc = RecordingController()
        server = BridgeServer(controller=rc, source=_race(), host="127.0.0.1", port=8794)
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8794")
            await client.connect()
            # angle_refresh=0: this test checks the CUT stream matches the pure director;
            # hold-time angle re-frames are exercised separately below.
            n = await drive_live(client, cfg=cfg, angle_refresh=0)
            await client.aclose()
            for _ in range(200):  # let the server apply any in-flight commands
                if len(rc.calls) >= n:
                    break
                await asyncio.sleep(0.01)
        return rc.calls, n

    calls, n = asyncio.run(go())
    assert n == len(decisions)
    assert len(calls) == len(decisions)
    for dec, cmd in zip(decisions, calls):
        assert cmd.op == CamOp.SWITCH_NUM
        assert cmd.car_number == numbers[dec.shot.target_idx].number
    assert any(d.shot.kind == ShotKind.INCIDENT for d in decisions)  # incident covered live


def test_long_hold_reframes_the_same_car_from_new_angles():
    """During a long hold on one subject the director re-frames the SAME car from fresh
    camera angles (extra commands beyond the cuts), so a lengthy battle/trouble hold
    isn't one flat static camera."""
    cfg = DirectorConfig()
    cuts = list(run_director(_race(), cfg))

    async def go(refresh):
        rc = RecordingController()
        server = BridgeServer(controller=rc, source=_race(), host="127.0.0.1", port=8795)
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8795")
            await client.connect()
            n = await drive_live(client, cfg=cfg, angle_refresh=refresh)
            await client.aclose()
            for _ in range(200):
                if len(rc.calls) >= n:
                    break
                await asyncio.sleep(0.01)
        return rc.calls

    calls = asyncio.run(go(2.0))               # aggressive re-frame -> extra angle changes
    # more commands land than there are subject cuts (the surplus are re-frames)...
    assert len(calls) > len(cuts)
    # ...and every re-frame is a switch to a car that was a genuine shot target (same car,
    # new angle: never an off-target cut).
    targets = {c.car_number for c in calls}
    shot_numbers = {_race().session_info().drivers_by_idx()[d.shot.target_idx].number for d in cuts}
    assert targets <= shot_numbers


def test_director_reconnects_after_the_bridge_drops():
    """The director is the one worker a race cannot afford to lose: if it dies, the
    in-sim camera freezes wherever it last pointed for the rest of the session. It used
    to exit on any bridge drop while the overlay worker self-healed.

    Here the sim-box bridge goes away mid-broadcast and comes back (an agent restart);
    the director must reconnect on its own and resume cutting cameras."""
    port = 8798

    async def go():
        first, second = RecordingController(), RecordingController()
        connects, drops = [], []
        task = asyncio.create_task(drive_live_forever(
            f"ws://127.0.0.1:{port}", cfg=DirectorConfig(), angle_refresh=0,
            reconnect=0.05, on_connect=lambda info: connects.append(info.track_name),
            on_disconnect=drops.append))

        async def serve_until(controller, n_cmds):
            server = BridgeServer(controller=controller, source=_race(),
                                  host="127.0.0.1", port=port, rate=400.0)
            async with server.ws_server():
                for _ in range(400):
                    if len(controller.calls) >= n_cmds:
                        return True
                    await asyncio.sleep(0.01)
            return False

        try:
            got_first = await serve_until(first, 2)   # bridge up, cutting cameras
            await asyncio.sleep(0.2)                  # ...bridge is DOWN in this window
            got_second = await serve_until(second, 2)  # agent restarts
        finally:
            task.cancel()
        return got_first, got_second, connects, drops

    got_first, got_second, connects, drops = asyncio.run(go())
    assert got_first, "director never drove the first bridge"
    assert got_second, "director did not reconnect after the bridge came back"
    assert len(connects) >= 2 and drops, "expected a drop and a fresh connect"


def test_bridge_fans_out_one_source_to_many_clients():
    """One source read, broadcast to all clients: the director and the overlay can
    share the same live stream. First client gets the whole thing; a later joiner
    gets a contiguous tail (correct live-stream semantics)."""
    async def go():
        server = BridgeServer(
            source=SyntheticSource(num_cars=6, duration_s=20.0, hz=10.0, seed=9),
            host="127.0.0.1", port=8797, rate=200.0)  # ~5ms/frame: slow enough to both join
        async with server.ws_server():
            a = BridgeClient("ws://127.0.0.1:8797")
            b = BridgeClient("ws://127.0.0.1:8797")
            await a.connect()
            await b.connect()
            a_ticks, b_ticks = [], []

            async def drain(client, out):
                async for fr in client.frames():
                    out.append(fr.tick)

            await asyncio.gather(drain(a, a_ticks), drain(b, b_ticks))
            await a.aclose()
            await b.aclose()
        return a_ticks, b_ticks

    a_ticks, b_ticks = asyncio.run(go())
    assert a_ticks == sorted(a_ticks) and len(a_ticks) > 10
    assert b_ticks and b_ticks == sorted(b_ticks)
    # both were fed from a single source read: b's frames are a tail of a's
    assert b_ticks == a_ticks[len(a_ticks) - len(b_ticks):]


def test_raw_broadcast_roundtrips_and_refuses_the_destructive_ids():
    """The diagnostic op. Its whole value is that what goes on the wire is EXACTLY what
    was asked for, so the round trip is the test, and erase_tape (6) and pit_command
    (9) are refused whatever they are wrapped in, because discovering either by
    accident during a broadcast is unrecoverable."""
    from pylon.camera.command import RAW_FORBIDDEN

    cmd = CamCommand.raw(12, 2, 5905700, 0, label="seek probe")
    assert CamCommand.from_dict(cmd.to_dict()) == cmd
    assert cmd.to_dict()["raw_args"] == [12, 2, 5905700, 0]

    rc = RecordingController()
    assert apply(rc, cmd) == {"ret": 1, "err": 0, "fake": True}
    assert rc.calls[0].raw_args == (12, 2, 5905700, 0)

    assert RAW_FORBIDDEN == frozenset({6, 9})
    for forbidden in sorted(RAW_FORBIDDEN):
        with pytest.raises(ValueError, match="forbidden"):
            apply(RecordingController(), CamCommand.raw(forbidden))


def test_the_agent_advertises_what_it_can_actually_do():
    """The sim-box agent is a --noconsole exe deployed by hand: no log, no version.
    Until now the only way to tell what was deployed was to send a command and infer
    the build from whether anything happened, which on 2026-07-25 cost an evening:
    a stale exe swallowed ten diagnostic messages and the tool reported a confident,
    completely wrong verdict about the sim. Capability is now stated, not inferred."""
    from pylon.camera.bridge import AGENT_PROTOCOL, controller_ops

    full = controller_ops(RecordingController())
    assert CamOp.RAW in full and CamOp.REPLAY_SEEK in full and CamOp.SWITCH_NUM in full

    class OldAgent:                       # a build from before the replay work
        def switch_num(self, *a): ...
        def switch_pos(self, *a): ...
        def set_state(self, *a): ...

    old = controller_ops(OldAgent())
    assert old == [CamOp.SET_STATE, CamOp.SWITCH_NUM, CamOp.SWITCH_POS]
    assert CamOp.RAW not in old and CamOp.REPLAY_SEEK not in old
    assert AGENT_PROTOCOL >= 2


def test_capabilities_reach_the_client_over_a_real_socket():
    async def go():
        server = BridgeServer(RecordingController(),
                              source=SyntheticSource(num_cars=4, duration_s=5.0, hz=10.0,
                                                     seed=1),
                              host="127.0.0.1", port=8798, rate=50.0)
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8798")
            await client.connect()
            agent, supports_raw = client.agent, client.agent_supports(CamOp.RAW)
            await client.aclose()
        return agent, supports_raw

    agent, supports_raw = asyncio.run(go())
    assert agent is not None, "the agent never said what it can do"
    assert agent["protocol"] >= 2
    assert agent["controller"] == "RecordingController"
    assert supports_raw is True

    # ...and a client talking to an agent too old to say anything must not assume
    stale = BridgeClient("ws://unused")
    assert stale.agent is None
    assert stale.agent_supports(CamOp.SWITCH_NUM) is True     # the original ops predate this
    assert stale.agent_supports(CamOp.RAW) is False           # anything newer: assume not
    assert stale.agent_supports(CamOp.REPLAY_SEEK) is False


# --- the show-config channel ------------------------------------------------
#
# Live settings (which cars to favour, and anything else that changes mid-broadcast)
# ride the bridge because it is already the
# one thing every worker is connected to. The properties that matter are that it MERGES
# rather than replaces, that it fans out to everyone, and that it is replayed on connect
# so a restarted worker comes back with the show still configured.

def _cfg_server(port):
    return BridgeServer(source=SyntheticSource(num_cars=6, duration_s=6.0, hz=10.0, seed=1),
                        host="127.0.0.1", port=port, rate=200.0)


def test_show_config_is_merged_and_fanned_out_to_every_client():
    async def go():
        server = _cfg_server(8801)
        async with server.ws_server():
            a, b = BridgeClient("ws://127.0.0.1:8801"), BridgeClient("ws://127.0.0.1:8801")
            await a.connect()
            await b.connect()

            seen_b = []
            b.on_show_config = seen_b.append

            async def drain(c, n=40):
                i = 0
                async for _ in c.frames():
                    i += 1
                    if i >= n:
                        return

            await a.send_show_config({"vips": ["64"]})
            await asyncio.gather(drain(a), drain(b))
            await a.send_show_config({"replays": False})
            await asyncio.gather(drain(a), drain(b))

            out = (dict(a.show_config), dict(b.show_config), list(seen_b))
            await a.aclose()
            await b.aclose()
            return out

    a_cfg, b_cfg, seen_b = asyncio.run(go())
    # merged, not replaced: changing one setting must not wipe every other
    assert a_cfg == {"vips": ["64"], "replays": False}
    assert b_cfg == a_cfg          # the client that never sent anything agrees
    assert len(seen_b) >= 2        # and was told each time, not left to poll


def test_show_config_is_replayed_to_a_worker_that_reconnects():
    """The restart-survival property. Without it, the supervisor bouncing a worker
    would silently unmute it and the director would forget the VIPs mid-race."""
    async def go():
        server = _cfg_server(8802)
        async with server.ws_server():
            setter = BridgeClient("ws://127.0.0.1:8802")
            await setter.connect()
            await setter.send_show_config({"vips": ["64", "17"]})

            i = 0
            async for _ in setter.frames():   # let the server apply it
                i += 1
                if i >= 20:
                    break
            await setter.aclose()

            late = BridgeClient("ws://127.0.0.1:8802")   # a worker restarting
            await late.connect()
            i = 0
            async for _ in late.frames():
                i += 1
                if i >= 20:
                    break
            cfg = dict(late.show_config)
            await late.aclose()
            return cfg

    assert asyncio.run(go()) == {"vips": ["64", "17"]}


def test_a_malformed_show_config_is_ignored_without_dropping_the_client():
    """It shares the socket with telemetry. Anything that can take this connection down
    takes the whole broadcast's data with it."""
    async def go():
        server = _cfg_server(8803)
        async with server.ws_server(), websockets.connect("ws://127.0.0.1:8803") as ws:
            await ws.recv()                                     # session
            await ws.send(json.dumps({"type": "showconfig", "config": "not a dict"}))
            await ws.send(json.dumps({"type": "showconfig"}))    # no config at all
            await ws.send(json.dumps({"type": "showconfig", "config": {"vips": ["7"]}}))
            for _ in range(200):
                msg = json.loads(await ws.recv())
                if msg.get("type") == "showconfig":
                    return msg["config"], server.show_config
        return None, None

    fanned, held = asyncio.run(go())
    assert fanned == {"vips": ["7"]}   # the good one still landed
    assert held == {"vips": ["7"]}     # and the junk left no trace


def test_show_config_never_surfaces_as_a_frame():
    """frames() is telemetry. A caller iterating it must not have to know that other
    message kinds share the socket."""
    async def go():
        server = _cfg_server(8804)
        async with server.ws_server():
            c = BridgeClient("ws://127.0.0.1:8804")
            await c.connect()
            await c.send_show_config({"vips": ["64"]})
            frames = []
            async for fr in c.frames():
                frames.append(fr)
                if len(frames) >= 30:
                    break
            cfg = dict(c.show_config)
            await c.aclose()
            return frames, cfg

    frames, cfg = asyncio.run(go())
    assert cfg == {"vips": ["64"]}                       # it did arrive
    assert all(hasattr(f, "session_time") for f in frames)  # ...as config, not as a frame


# --- the live loop's own wiring: what drive_live adds around the pure director -------

def _race5():
    """The rotating race test_director uses for the hold: a grid busy enough to cut often."""
    return SyntheticSource(num_cars=14, duration_s=120.0, hz=10.0, seed=5)


def test_the_shot_file_heartbeat_follows_wall_time_while_session_time_is_frozen(monkeypatch):
    """A paused tape freezes session time; the reader's staleness is wall time. The
    heartbeat has to run on the reader's clock, or the file goes stale mid-pause and the
    overlay loses the shot the camera is still sitting on."""
    from pylon.show import live as drv

    written = []
    monkeypatch.setattr(drv, "write_shot", lambda shot, replay=None: written.append(shot))

    class Frozen:
        """The first 40 frames of a race, then the last one repeated with its clock stopped."""

        def __init__(self):
            self._src = _race()

        def session_info(self):
            return self._src.session_info()

        def frames(self):
            last = None
            for i, f in enumerate(self._src.frames()):
                if i >= 40:
                    break
                last = f
                yield f
            for _ in range(60):
                yield last

    wall = [0.0]

    def clock():
        wall[0] += 0.1
        return wall[0]

    async def go():
        server = BridgeServer(controller=RecordingController(), source=Frozen(),
                              host="127.0.0.1", port=8789)
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8789")
            await client.connect()
            await drive_live(client, cfg=DirectorConfig(), angle_refresh=0, refresh_every=0,
                             clock=clock)
            await client.aclose()

    asyncio.run(go())
    assert len(written) >= 2, written                  # the cut, then heartbeats
    assert all(s == written[0] for s in written)      # the held shot, never another


def test_the_live_director_learns_a_car_that_joined_after_it_connected():
    """The actuator's car numbers come from DriverInfo at connect. A late joiner has no
    number, so every shot of it is dropped on the floor while the director believes it
    has cut. drive_live re-reads the roster and the actuator adopts it."""
    full = _race().session_info()
    drivers = full.raw["DriverInfo"]["Drivers"]
    joiner = drivers[-1]["CarIdx"]

    class Roster:
        """Session info that grows on the second read: the first client sees the grid
        without the last car, the next one (the refresh) sees everyone."""

        def __init__(self):
            self._src, self.reads = _race(), 0

        def session_info(self):
            self.reads += 1
            if self.reads == 1:
                return SessionInfo({**full.raw, "DriverInfo": {"Drivers": drivers[:-1]}})
            return full

        def frames(self):
            return self._src.frames()

    async def go():
        roster = Roster()
        # paced, so the refresh lands while the race is still streaming
        server = BridgeServer(controller=RecordingController(), source=roster,
                              host="127.0.0.1", port=8788, rate=100.0)
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8788")
            info = await client.connect()
            assert joiner not in info.drivers_by_idx()
            actuator = Actuator(info)
            assert joiner not in actuator.numbers
            await drive_live(client, cfg=DirectorConfig(), actuator=actuator,
                             angle_refresh=0, refresh_every=0.05, limit=20.0)
            await client.aclose()
        return actuator, roster.reads

    actuator, reads = asyncio.run(go())
    assert reads >= 2
    assert actuator.numbers[joiner] == drivers[-1]["CarNumber"]


def test_actuator_refresh_resolves_a_number_it_did_not_have():
    before = _session_with_cams()
    act = Actuator(before, ActuatorConfig(policy=_TV1_ONLY))
    late = Shot(ShotKind.BATTLE, "battle:2:5", 5, "#99 v #7", pair=(2, 5))
    assert act.command_for(late) is None                # unknown car: nothing to send

    after = SessionInfo({**before.raw, "DriverInfo": {"Drivers": [
        *before.raw["DriverInfo"]["Drivers"], {"CarIdx": 5, "CarNumber": "99"}]}})
    act.refresh_info(after)
    cmd = act.command_for(late)
    assert cmd is not None and cmd.car_number == "99"
    assert cmd.group == 11                               # the map survived the refresh


# --- the bridge's failure model ------------------------------------------------------

def test_a_pump_that_dies_ends_the_stream_instead_of_wedging_the_client():
    """An exception in the pump task used to be swallowed: no `end`, sockets open,
    keepalive pings answered by the library, and the director sat in `async for` for
    the rest of the race while the studio's port probe called the bridge healthy."""

    class Dies:
        def session_info(self):
            return _race().session_info()

        def frames(self):
            for i, f in enumerate(_race().frames()):
                if i == 3:
                    raise RuntimeError("the mmap went away")
                yield f

    async def go():
        server = BridgeServer(controller=RecordingController(), source=Dies(),
                              host="127.0.0.1", port=8787)
        async with server.ws_server():
            client = BridgeClient("ws://127.0.0.1:8787")
            await client.connect()
            got = []

            async def read():
                async for fr in client.frames():
                    got.append(fr.tick)

            await asyncio.wait_for(read(), timeout=5.0)     # hung forever before
            await client.aclose()
        return got, server.pump_error

    got, err = asyncio.run(go())
    assert got == [0, 1, 2]
    assert isinstance(err, RuntimeError)


def test_a_silent_bridge_times_out_instead_of_hanging_forever():
    """Belt and braces for the case above: a server that is alive but has stopped
    talking. The client gives up after read_timeout, and every driver loop treats that
    TimeoutError as a dropped bridge and reconnects."""

    async def handler(ws):
        await ws.send(json.dumps({"type": "session", "session_info": {}}))
        await asyncio.sleep(10)                           # then nothing, ever

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 8786):
            client = BridgeClient("ws://127.0.0.1:8786", read_timeout=0.3)
            await client.connect()
            with pytest.raises(TimeoutError):
                async for _ in client.frames():
                    pass
            await client.aclose()

    asyncio.run(go())


def test_a_client_that_stops_reading_does_not_stall_the_others():
    """The frame is shared, the fan-out is per client, and one wedged reader must not
    hold the director's telemetry. A dead reader piles up in its own buffer and is cut
    by the keepalive; the live reader sees every frame, on time."""

    async def go():
        src = SyntheticSource(num_cars=40, duration_s=60.0, hz=10.0, seed=3)   # ~6 MB
        server = BridgeServer(controller=RecordingController(), source=src,
                              host="127.0.0.1", port=8785)
        async with server.ws_server():
            dead = await websockets.connect("ws://127.0.0.1:8785")   # never reads
            live = BridgeClient("ws://127.0.0.1:8785")
            await live.connect()
            n = 0

            async def read():
                nonlocal n
                async for _ in live.frames():
                    n += 1

            await asyncio.wait_for(read(), timeout=10.0)
            await live.aclose()
            await dead.close()
        return n

    assert asyncio.run(go()) >= 590


# --- the package's import promise ---------------------------------------------

def test_the_camera_package_imports_without_the_brain():
    """camera/ is protocol, transport and angles. The loop that runs the director over it
    is show/live.py, and nothing in camera/ may import the director or the world model
    back: the vocabulary the angle policy needs (ShotKind, ShotFlavor) is show/contract.py,
    which imports nothing. Until 2026-09 `import pylon.camera.bridge`
    executed the package init, which re-exported the show loop and so loaded the
    director, the world model, the story and the shot files behind a transport whose
    docstring promised the opposite. Only a clean interpreter can see this: every other
    test has already imported the world."""
    import subprocess
    import sys

    script = ("import sys; import pylon.camera; "
              "brain = sorted(m for m in sys.modules if m.startswith('pylon.') "
              "and (m.split('.')[1] in ('director', 'world', 'overlay') "
              "or m in ('pylon.show.live', 'pylon.show.shotlink'))); "
              "print(brain)")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0, f"camera does not import standalone:\n{r.stderr}"
    assert r.stdout.strip() == "[]", f"importing camera loaded the brain: {r.stdout}"
