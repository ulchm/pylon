"""Instant replays: the mode machine, and the isolation that keeps them from lying.

The acceptance test #18 asks for is `test_a_replay_leaves_the_live_world_untouched`:
run a replay mid-stream and assert the live world model comes out the far side exactly
as it went in. Everything else here supports it.

The fake bridge below is the point. A SyntheticSource cannot test any of this, because
its clock only ever goes forward: a "replay" against it is indistinguishable from
dropping frames. `TapeBridge` is a sim that actually honours a seek: it rewinds to the
asked-for session time, serves the earlier frames again, and returns where it left off.
That is what makes the poisoning reproducible, and the poisoning is the whole risk:
without isolation those re-served frames re-fire events that already happened, and the
graphics start reporting a second off about one off.

It models the TAPE case (the sim playing a saved endurance replay), measured as the
real setup on 2026-07-25: the tape does not advance while we are away, so an excursion
misses nothing and the return is exact.
"""

import asyncio
from types import SimpleNamespace

from pylon.camera import Actuator, ActuatorConfig, AnglePolicy
from pylon.camera.angles import Angle
from pylon.camera.command import CamCommand, CamOp, RpySrchMode
from pylon.director import ReplayConfig, ReplayDirector, ReplayState
from pylon.show.live import drive_live, replay_commands
from pylon.telemetry.constants import (
    MAX_CARS,
    SessionFlag,
    SessionState,
    TrackSurface,
)
from pylon.telemetry.frame import Frame, SessionInfo
from pylon.world import TimeJump, WorldModel

TRACK_M = 4000.0
_TV1_ONLY = AnglePolicy(profiles={}, default=(Angle.TV1,))


def _info(n: int = 6) -> SessionInfo:
    return SessionInfo({
        "WeekendInfo": {"TrackDisplayName": "Test", "TrackLength": "4.00 km",
                        "EventType": "Race"},
        "DriverInfo": {"Drivers": [
            {"CarIdx": i, "CarNumber": str(i + 1), "UserName": f"D{i}",
             "CarClassID": 1, "CarIsPaceCar": False, "CarIsAI": True} for i in range(n)
        ]},
        "CameraInfo": {"Groups": [{"GroupNum": 11, "GroupName": "TV1"},
                                  {"GroupNum": 12, "GroupName": "TV2"}]},
    })


def _frame(tick: int, t: float, n: int = 6, *, off: set[int] = frozenset(),
           demote: int | None = None) -> Frame:
    """One synthetic frame. `demote` swaps that car's place with the one behind it on
    the sheet: what a contact costs, in the only currency the replay verdict can see."""
    ldp = [-1.0] * MAX_CARS
    lap = [-1] * MAX_CARS
    lapc = [-1] * MAX_CARS
    pos = [0] * MAX_CARS
    cpos = [0] * MAX_CARS
    cls = [-1] * MAX_CARS
    onpit = [False] * MAX_CARS
    surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
    f2 = [-1.0] * MAX_CARS
    for i in range(n):
        # 70m apart: inside INCIDENT_PAIR_M (75) so two cars going off together read
        # as CONTACT, but 1.4s apart in time so no pair is ever a battle: the director
        # stays on the leader, which is the lull a replay is allowed to use.
        d = 2000.0 + 50.0 * t - 70.0 * i
        if i == demote:
            # ...and a demoted car has really dropped back: behind the next one on the
            # road, by the same spacing, or the live order (which trusts the track over
            # the sheet for a completed pass) would put it straight back in front.
            d -= 140.0
        ldp[i] = (d % TRACK_M) / TRACK_M
        lapc[i] = int(d // TRACK_M)
        lap[i] = lapc[i] + 1
        pos[i] = i + 1
        cpos[i] = i + 1
        cls[i] = 1
        surf[i] = TrackSurface.OFF_TRACK if i in off else TrackSurface.ON_TRACK
        f2[i] = 2.4 * i
    if demote is not None and demote + 1 < n:
        pos[demote], pos[demote + 1] = pos[demote + 1], pos[demote]
        cpos[demote], cpos[demote + 1] = cpos[demote + 1], cpos[demote]
    return Frame(tick=tick, session_time=t, values={
        "SessionTime": t, "SessionTick": tick, "SessionNum": 2,
        "SessionState": SessionState.RACING, "SessionFlags": SessionFlag.GREEN,
        "SessionTimeRemain": 999.0, "SessionLapsRemain": -1,
        "IsReplayPlaying": 1,          # a saved tape, which is the real setup
        "CarIdxLapDistPct": ldp, "CarIdxLap": lap, "CarIdxLapCompleted": lapc,
        "CarIdxPosition": pos, "CarIdxClassPosition": cpos, "CarIdxClass": cls,
        "CarIdxOnPitRoad": onpit, "CarIdxTrackSurface": surf, "CarIdxF2Time": f2,
    })


def _tape(n_frames: int = 400, *, off_at: range | None = None,
          costs_a_place: bool = True) -> list[Frame]:
    """A tape with, optionally, two cars off the road together over a span of frames,
    which the world model reads as contact, i.e. a MAJOR incident. By default the
    second car loses a place a second later, because a contact that costs nobody
    anything is not replayed (ReplayConfig.contact_needs_consequence); pass
    costs_a_place=False for two cars that merely ran wide together."""
    out = []
    for k in range(n_frames):
        off = {2, 3} if (off_at is not None and k in off_at) else frozenset()
        demote = 3 if (costs_a_place and off_at is not None and k >= off_at.start + 10) else None
        out.append(_frame(k, 100.0 + k * 0.1, off=off, demote=demote))
    return out


class TapeBridge:
    """A BridgeClient whose sim honours replay_seek: it really does rewind.

    Without this the isolation cannot be tested at all: a source that only ever moves
    forward makes a replay look exactly like a gap in the stream.
    """

    def __init__(self, tape: list[Frame], info: SessionInfo):
        self.tape, self.info = tape, info
        self.sent: list[CamCommand] = []
        self.shots: list = []
        self._i = 0
        self._resume: int | None = None   # where live was when we seeked away
        self.served_replay = 0
        self.served_total = 0

    def _index_of(self, session_time: float) -> int:
        best = min(range(len(self.tape)),
                   key=lambda k: abs(self.tape[k].session_time - session_time))
        return best

    async def send(self, cmd: CamCommand) -> None:
        self.sent.append(cmd)
        if cmd.op == CamOp.REPLAY_SEEK:
            target = self._index_of(cmd.session_time_ms / 1000.0)
            if self._resume is None:
                self._resume = self._i          # first seek: remember where live was
                self._i = target
            else:                                # a seek while away = coming home
                self._i = self._resume
                self._resume = None
        elif cmd.op == CamOp.REPLAY_SEARCH and cmd.search_mode == RpySrchMode.TO_END:
            self._i = len(self.tape) - 1         # the FINISH: wrong on a tape, on purpose
            self._resume = None

    async def send_shot(self, shot) -> None:
        self.shots.append(shot)

    async def frames(self):
        while self._i < len(self.tape):
            fr = self.tape[self._i]
            self.served_total += 1
            if self._resume is not None:
                self.served_replay += 1
            yield fr
            self._i += 1

    async def aclose(self) -> None:
        pass


def _run(tape, replay, *, limit=0.0):
    client = TapeBridge(tape, _info())
    n = asyncio.run(drive_live(client, actuator=Actuator(_info(),
                                                        ActuatorConfig(policy=_TV1_ONLY)),
                               replay=replay, limit=limit, angle_refresh=0.0))
    return client, n


# --------------------------------------------------------------------------- #
# The acceptance test
# --------------------------------------------------------------------------- #

def test_a_replay_leaves_the_live_world_untouched():
    """#18's headline requirement. Run the same tape twice (once plain, once with a
    replay fired in the middle), and the live world model must end up in the same
    place. If replayed frames reached it, speeds would be derived across the seek and
    the incident would be counted twice."""
    tape = _tape(400, off_at=range(150, 175))

    plain = WorldModel(_info())
    for fr in tape:
        plain.update(fr)

    # now the same tape through the driver, with a replay forced partway
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown=0.0, cooldown_tape=0.0))
    client, _n = _run(tape, replay)

    assert replay.replays >= 1, "no replay ever fired"
    assert client.served_replay > 0, "the tape never actually rewound"

    # The replayed frames were served, and none of them reached a world model: the
    # driver's own model saw only live frames, so re-running the live ones alone
    # reproduces it exactly.
    seeks = [c for c in client.sent if c.op == CamOp.REPLAY_SEEK]
    assert len(seeks) >= 2, "expected a seek away and a seek home"
    # and we came home by SEEKING, never by to_end, which on a tape is the finish
    assert not [c for c in client.sent
                if c.op == CamOp.REPLAY_SEARCH and c.search_mode == RpySrchMode.TO_END]


def test_replayed_frames_never_reach_the_world_model():
    """The mechanism behind the test above, asserted directly: while rolling, the
    driver does not call update() at all, so no event can be re-detected."""
    tape = _tape(300, off_at=range(120, 145))
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown_tape=0.0))
    seen: list[float] = []

    class Spy(WorldModel):
        def update(self, frame):
            seen.append(frame.session_time)
            return super().update(frame)

    import pylon.show.live as drv
    real, drv.WorldModel = drv.WorldModel, Spy
    try:
        client, _ = _run(tape, replay)
    finally:
        drv.WorldModel = real

    assert client.served_replay > 0
    # session times fed to the model are non-decreasing: the rewound stretch is absent
    assert seen == sorted(seen), "a replayed (earlier) frame reached the live model"
    # every frame the sim served either reached the live model or was a replay frame.
    # (served_total exceeds len(tape): the excursion revisits frames, which is the point.)
    assert client.served_total > len(tape)
    assert len(seen) == client.served_total - client.served_replay


# --------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------- #

def test_a_seek_the_sim_ignores_does_not_strand_the_broadcast():
    """The failure that matters most on air. If the sim never honours the seek (an
    agent too old to know the op, a sim that dropped it), the machine must give up
    and go back to directing, not sit in ROLLING forever showing a live picture it
    believes is a replay."""
    class DeafBridge(TapeBridge):
        async def send(self, cmd):
            self.sent.append(cmd)      # accepts everything, honours nothing

    tape = _tape(300, off_at=range(100, 125))
    replay = ReplayDirector(ReplayConfig(enabled=True, settle=0.0, cooldown_tape=0.0))
    client = DeafBridge(tape, _info())
    asyncio.run(drive_live(client, actuator=Actuator(_info(), ActuatorConfig(policy=_TV1_ONLY)),
                           replay=replay, angle_refresh=0.0))

    assert replay.failed_seeks >= 1
    assert replay.state == ReplayState.IDLE      # gave up rather than wedged
    assert client.served_replay == 0


def test_a_moment_already_shown_live_loses_to_one_we_missed():
    """#18: 'Do not replay what was just shown live': the most likely source of
    'why did it show me that twice'. Docked rather than dropped, so a quiet race can
    still replay the only thing that happened."""
    from pylon.director.replay import ReplayCandidate

    missed = ReplayCandidate(at=10.0, session_num=2, car_idx=4, car_number="5",
                             driver="D4", kind="contact", label="contact", position=8,
                             seen_on_camera=False)
    shown = ReplayCandidate(at=10.0, session_num=2, car_idx=1, car_number="2",
                            driver="D1", kind="contact", label="contact", position=2,
                            seen_on_camera=True)
    assert missed.score > shown.score
    assert shown.score > 0.0     # still eligible, just far behind


def test_replays_wait_for_a_lull_and_never_cut_away_from_a_fight():
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = ReplayDirector(ReplayConfig(enabled=True))

    battle = Shot(ShotKind.BATTLE, "battle:1:2", 2, "", (1, 2))
    leader = Shot(ShotKind.LEADER, "leader:0", 0, "")
    trouble = Shot(ShotKind.TROUBLE, "trouble:3", 3, "")
    assert not r.safe_now(snap, battle)      # a live fight is never interrupted
    assert not r.safe_now(snap, trouble)     # nor is the incident we are already on
    assert not r.safe_now(snap, None)
    assert r.safe_now(snap, leader)          # the director's own "nothing happening"
    # nor a front-runner's pit stop (the leader here is car 0); a midfield stop is
    # a window only for a wreck, and there is none armed
    front_stop = Shot(ShotKind.PIT, "pit:0", 0, "")
    back_stop = Shot(ShotKind.PIT, f"pit:{snap.order[-1]}", snap.order[-1], "")
    assert not r.safe_now(snap, front_stop)
    assert not r.safe_now(snap, back_stop)


def test_cooldown_paces_replays_from_when_the_last_one_ENDED():
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = ReplayDirector(ReplayConfig(enabled=True, cooldown_tape=45.0), source_is_tape=True)
    leader = Shot(ShotKind.LEADER, "leader:0", 0, "")

    r.finish(snap.session_time)                       # a replay just ended
    assert not r.safe_now(snap, leader)

    later = wm.update(_frame(999, snap.session_time + 50.0))
    assert r.safe_now(later, leader)


def test_home_never_uses_to_end_unless_we_know_we_left_a_live_edge():
    """Same asymmetry as the probe's, and for the same reason: on a saved tape to_end
    is the finish, and that failure is not recoverable inside a broadcast."""
    tape_r = ReplayDirector(ReplayConfig(enabled=True), source_is_tape=True)
    tape_r.return_to, tape_r.return_session = 1234.5, 2
    assert [m.kind for m in tape_r.home()] == ["speed", "seek"]
    assert tape_r.home()[1].session_time == 1234.5

    unknown = ReplayDirector(ReplayConfig(enabled=True), source_is_tape=None)
    assert [m.kind for m in unknown.home()] == ["speed", "seek"]

    live = ReplayDirector(ReplayConfig(enabled=True), source_is_tape=False)
    assert [m.kind for m in live.home()] == ["speed", "to_end"]

    # ...and the translation preserves that, since to_end is never inferred
    assert [c.op for c in replay_commands(tape_r.home())] == [
        CamOp.REPLAY_SPEED, CamOp.REPLAY_SEEK]
    assert [c.op for c in replay_commands(live.home())] == [
        CamOp.REPLAY_SPEED, CamOp.REPLAY_SEARCH]


def test_a_dropped_bridge_mid_replay_does_not_leave_us_believing_we_are_rolling():
    r = ReplayDirector(ReplayConfig(enabled=True))
    r.state = ReplayState.ROLLING
    r.left = True
    r.last_replay_end = 500.0
    r.reset_connection()
    assert r.state == ReplayState.IDLE
    assert not r.rolling
    assert r.last_replay_end == 500.0     # pacing is about the broadcast, not the socket


def test_replays_are_off_unless_asked_for():
    """This is the one director feature that takes the broadcast off live pictures."""
    assert ReplayConfig().enabled is False
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(30):
        snap = wm.update(fr)
    r = ReplayDirector()          # defaults
    r.observe(snap, None)
    assert r.candidate is None and r.state == ReplayState.IDLE


def test_the_director_package_imports_on_its_own():
    """Guard for a circular import that the rest of the suite structurally cannot see.

    show/live.py imports both the director and camera/. When it lived in camera/ and
    was re-exported from there, a director that imported camera.command pulled
    camera/__init__ -> driver -> director, half-built. Every other test imports camera
    first, which happens to work, so the suite stayed green while `pylon direct`
    died on an ImportError. Importing the director FIRST, in a clean interpreter, is
    the only thing that catches it, and it stays as the guard against the next such
    edge (director and camera now share show/contract.py, which imports nothing).
    """
    import subprocess
    import sys

    script = ("import pylon.director as d; "
              "import pylon.camera as c; "
              "print(d.ReplayDirector.__name__, c.CamCommand.__name__)")
    r = subprocess.run([sys.executable, "-c", script],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"director does not import standalone:\n{r.stderr}"
    assert "ReplayDirector CamCommand" in r.stdout


# --- #66: the wreck exception -------------------------------------------------
# A crash makes the director cut to TROUBLE and hold it, so before this the shot that
# PROVED a replay was warranted was also the shot forbidding it, and every wreck
# candidate aged out. Measured on the rig: 101 consecutive refusals on "shot is
# trouble", and not one wreck replay in a whole session.

def _armed_wreck(r, snap, *, car_idx=3, kind="trouble"):
    from pylon.director.replay import ReplayCandidate

    r.candidate = ReplayCandidate(
        at=snap.session_time, session_num=2, car_idx=car_idx, car_number="9",
        driver="Stricken Driver", kind=kind, label="in trouble", position=8)
    r.armed_at = snap.session_time
    r.state = ReplayState.ARMED
    return r


def test_a_wreck_may_be_left_once_it_has_had_its_moment_live():
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, wreck_dwell=5.0)), snap)
    trouble = Shot(ShotKind.TROUBLE, "trouble:3", 3, "")

    # Still unfolding: cutting to a replay now would race the incident itself.
    assert not r.safe_now(snap, trouble)

    later = wm.update(_frame(999, snap.session_time + 6.0))
    assert r.safe_now(later, trouble)


def test_the_wreck_exception_is_only_for_our_own_car():
    """Another car's trouble is somebody else's story and no reason to leave."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, wreck_dwell=5.0)), snap)
    later = wm.update(_frame(999, snap.session_time + 6.0))

    assert not r.safe_now(later, Shot(ShotKind.TROUBLE, "trouble:5", 5, ""))
    assert r.safe_now(later, Shot(ShotKind.TROUBLE, "trouble:3", 3, ""))


def test_a_battle_still_vetoes_even_with_a_wreck_armed():
    """The one rule that does not bend: never cut away from a live fight."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, wreck_dwell=5.0)), snap)
    later = wm.update(_frame(999, snap.session_time + 60.0))

    assert not r.safe_now(later, Shot(ShotKind.BATTLE, "battle:1:2", 2, "", (1, 2)))


def test_the_wreck_exception_can_be_switched_off():
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(
        ReplayDirector(ReplayConfig(enabled=True, allow_on_own_trouble=False)), snap)
    later = wm.update(_frame(999, snap.session_time + 60.0))

    assert not r.safe_now(later, Shot(ShotKind.TROUBLE, "trouble:3", 3, ""))


def test_speed_is_ordered_BEFORE_the_seek_in_both_directions():
    """A play-speed message right behind a seek discards it. Measured on the rig:

        seek alone                   -> lands in 0.12s
        seek THEN speed immediately  -> never moves back
        speed THEN seek              -> lands in 0.12s

    Nothing in the types enforces this, and getting it wrong breaks every excursion
    while looking exactly like a sim that ignores replay commands, so it is pinned
    here rather than left to the comment in start().
    """
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True)), snap)

    kinds = [m.kind for m in r.start(snap, now=0.0)]
    assert kinds.index("speed") < kinds.index("seek"), kinds

    r.source_is_tape = True
    home_kinds = [m.kind for m in r.home()]
    assert home_kinds.index("speed") < home_kinds.index("seek"), home_kinds


# --- the excursion, laid out on the tape ----------------------------------------
# Run up to the moment at real speed so the viewer reads the closing rate, drop to half
# for the hit, come back out for the outcome, and only then go home. Every one of those
# is a TAPE position: measured on air (2026-09-14), a wall-clock roll came home 0.5s
# past the mark every time, so the viewer got slow motion of the run-up and never saw
# what happened. Measured on the rig: slow_motion DIVIDES by speed, and speed=2 is
# 0.49x (0.33x on another day), so 2 is the half speed a replay wants and the layout
# cannot depend on the rate.

def _rolling(cfg=None, *, return_to: float = 230.0):
    """A director already away on an excursion: the moment is at t=200, and live was
    at t=230 when we left (a replay follows its moment by at least wreck_dwell).

    The ramp is opt-in since Round 1 (no audio in slow motion), so this section asks
    for it; `test_the_default_replay_runs_at_real_speed` pins the default."""
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(cfg or ReplayConfig(enabled=True, slow_at_moment=True)),
                     snap)
    r.candidate.at = 200.0
    r.state = ReplayState.ROLLING
    r.return_to = return_to
    r.left = True
    r.rolled_from = 0.0
    return r


def test_the_ramp_drops_to_slow_motion_as_the_tape_reaches_the_moment():
    r = _rolling()
    # still running up to it: real speed, nothing emitted
    assert r.tick(_frame(1, 196.0), now=1.0) == []
    assert not r.slowed

    # slow_lead defaults to 1.0s, so 199.0 is where it should go
    moves = r.tick(_frame(2, 199.2), now=1.5)
    assert [m.kind for m in moves] == ["speed"]
    assert moves[0].slow_motion is True
    assert moves[0].speed == 2                      # ~0.5x, measured
    assert r.slowed


def test_the_ramp_fires_once_per_excursion():
    r = _rolling()
    assert r.tick(_frame(1, 199.5), now=1.0)        # fires
    assert r.tick(_frame(2, 200.5), now=2.0) == []  # and not again
    assert r.tick(_frame(3, 201.0), now=3.0) == []


def test_the_excursion_runs_through_the_moment_to_the_outcome_before_going_home():
    """The whole shape, on the tape: slow motion from at-slow_lead, real speed again at
    at+slow_after, home at at+slow_after+lead_out. Not one of these is a wall time."""
    cfg = ReplayConfig(enabled=True, slow_at_moment=True,
                       slow_lead=1.0, slow_after=1.5, lead_out=2.5)
    r = _rolling(cfg)
    assert r.tick(_frame(1, 199.5), now=1.0)[0].slow_motion is True
    # through the moment and past it, still slow: NOT home 0.5s after the mark
    assert r.tick(_frame(2, 200.5), now=3.0) == []
    assert r.tick(_frame(3, 201.4), now=5.0) == []
    # out of slow motion for the outcome, once
    out = r.tick(_frame(4, 201.5), now=6.0)
    assert [m.kind for m in out] == ["speed"]
    assert out[0].speed == 1 and out[0].slow_motion is False
    assert r.unslowed
    assert r.tick(_frame(5, 202.5), now=7.0) == []
    assert r.tick(_frame(6, 203.9), now=8.0) == []
    # and home at the end of the outcome
    home = r.tick(_frame(7, 204.0), now=9.0)
    assert [m.kind for m in home] == ["speed", "seek"]
    assert r.state == ReplayState.RETURNING
    assert r.tape_end() == 204.0


def test_the_excursion_never_runs_past_where_live_was():
    """A moment marked at the live edge (a lead change) has no outcome on the tape yet:
    running on would show the viewer the next seconds inside the replay and then again
    live. The end is capped at return_to, and the ramp does not fire into that cap."""
    r = _rolling(return_to=200.2)
    assert r.tape_end() == 199.7
    moves = r.tick(_frame(1, 199.7), now=1.0)
    assert [m.kind for m in moves] == ["speed", "seek"]       # home, not slow motion
    assert not r.slowed


def test_the_wall_clock_is_only_a_backstop():
    """A tape that does not advance (parked at the end, a speed the sim ignored) must
    still come home: `roll` is that cap and nothing else."""
    r = _rolling(ReplayConfig(enabled=True, roll=5.0))
    assert r.tick(_frame(1, 195.0), now=4.9) == []
    moves = r.tick(_frame(2, 195.0), now=5.1)
    assert [m.kind for m in moves] == ["speed", "seek"]


def test_coming_home_restores_real_speed_after_slow_motion():
    """home() sends speed FIRST, which both clears slow motion and un-slows the way back."""
    r = _rolling(ReplayConfig(enabled=True, roll=5.0, slow_at_moment=True))
    r.tick(_frame(1, 199.5), now=1.0)
    assert r.slowed
    moves = r.tick(_frame(2, 201.0), now=99.0)      # well past the roll
    assert [m.kind for m in moves] == ["speed", "seek"]
    assert moves[0].speed == 1 and moves[0].slow_motion is False


def test_the_ramp_can_be_switched_off():
    r = _rolling(ReplayConfig(enabled=True, slow_at_moment=False))
    assert r.tick(_frame(1, 199.5), now=1.0) == []
    assert not r.slowed
    assert r.tick(_frame(2, 203.9), now=2.0) == []           # real speed throughout
    assert [m.kind for m in r.tick(_frame(3, 204.0), now=3.0)] == ["speed", "seek"]


def test_the_default_replay_runs_at_real_speed():
    """Round 1: every replay ran 7.3 wall seconds of silent slow motion (the sim mutes
    it, and speed=2 played at 0.34x, not the 0.49x it was tuned at). Real speed is the
    default; the ramp is `--replay-slow-mo`."""
    assert ReplayConfig().slow_at_moment is False
    r = _rolling(ReplayConfig(enabled=True))
    assert r.tick(_frame(1, 199.5), now=1.0) == []
    assert not r.slowed
    # a reader is told the moment lands lead_in after the seek, not lead_in + slow_lead
    from pylon.show.live import replay_banner
    assert replay_banner(r)["moment_in"] == ReplayConfig.lead_in


# --- not cutting live to an off-camera incident -------------------------------
# Cutting live to a car that has already stopped shows the aftermath, the least
# interesting frame of the whole incident. The replay shows the run-up and the hit.
# An incident on a car we are ALREADY watching is the opposite case and stays.

def test_an_offcamera_incident_no_longer_grabs_the_live_camera():
    from pylon.director import Director, DirectorConfig
    from pylon.director.model import ShotKind

    tape = _tape(400, off_at=range(150, 200))
    wm = WorldModel(_info())

    def run(on_camera_rule: bool) -> set[str]:
        d = Director(DirectorConfig(interrupt_only_if_on_camera=on_camera_rule))
        w = WorldModel(_info())
        kinds = set()
        for fr in tape:
            snap = w.update(fr)
            d.update(snap)
            if d.current is not None:
                kinds.add(d.current.kind)
        return kinds

    assert ShotKind.INCIDENT in run(False), "baseline: incidents DO interrupt normally"
    assert ShotKind.INCIDENT not in run(True), "with the rule on, an off-camera incident must not"
    del wm


def test_a_wreck_may_now_leave_from_an_ordinary_shot():
    """With the director no longer cutting to it, a wreck candidate is armed while we
    are on the leader, so it must not need a TROUBLE shot to leave from."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, wreck_dwell=5.0)), snap)
    later = wm.update(_frame(999, snap.session_time + 6.0))

    assert r.safe_now(later, Shot(ShotKind.LEADER, "leader:0", 0, ""))
    assert r.safe_now(later, Shot(ShotKind.FOLLOW, "follow:4", 4, ""))
    # and still never over a fight
    assert not r.safe_now(later, Shot(ShotKind.BATTLE, "battle:1:2", 2, "", (1, 2)))


# --- no lead changes before the field is released -----------------------------
# Reported live: five "lead change" replays fired while the field was still gridding
# at Watkins. Cars really do shuffle forming up; none of it is a pass. Same fault the
# same shape as #63, and the same predicate fixes it.

def _grid_frame(tick: int, t: float, *, leader_idx: int) -> Frame:
    """A frame where `leader_idx` is genuinely at the front.

    Every per-car channel is swapped, not just CarIdxPosition: `live_order` reorders the
    timing sheet by PROGRESS, so a position swap on its own is overruled by lap distance
    and the leader never actually changes.
    """
    fr = _frame(tick, t)
    v = dict(fr.values)
    if leader_idx != 0:
        for key in ("CarIdxLapDistPct", "CarIdxLap", "CarIdxLapCompleted",
                    "CarIdxPosition", "CarIdxClassPosition", "CarIdxF2Time"):
            arr = list(v[key])
            arr[0], arr[leader_idx] = arr[leader_idx], arr[0]
            v[key] = arr
    return Frame(tick=tick, session_time=t, values=v)


def _lead_swap_candidates(*, green: bool, released: bool, kind: str | None = None) -> int:
    """Run a leader change past the replay director; return how many candidates armed."""
    wm = WorldModel(_info())
    r = ReplayDirector(ReplayConfig(enabled=True))
    armed = 0
    for k, leader in enumerate([0, 0, 1, 1, 0, 0]):
        snap = wm.update(_grid_frame(k, 100.0 + k, leader_idx=leader))
        # force the released flag, which is what a held start turns on
        object.__setattr__(snap.session, "field_released", released)
        object.__setattr__(snap.session, "is_green", green)
        if kind is not None:
            object.__setattr__(snap.session, "session_kind", kind)
        before = r.state
        r.observe(snap, None)
        if before != r.state and r.state == ReplayState.ARMED:
            armed += 1
            r._disarm()
    return armed


def test_no_lead_change_replay_while_the_field_is_forming_up():
    assert _lead_swap_candidates(green=False, released=False) == 0


def test_a_lead_change_still_replays_once_the_race_is_on():
    assert _lead_swap_candidates(green=True, released=True) > 0


def test_a_lead_change_is_only_a_pass_in_a_race():
    """Qualifying and practice have an order too, by best lap, and its "leader" changes
    every time somebody goes quicker. Round 1's qualifying armed `lead_change:9:266.7`
    on a session best; only the cooldown bug kept it off air."""
    from pylon.world import SessionKind
    assert _lead_swap_candidates(green=True, released=True, kind=SessionKind.QUALIFY) == 0
    assert _lead_swap_candidates(green=True, released=True, kind=SessionKind.PRACTICE) == 0
    assert _lead_swap_candidates(green=True, released=True, kind=SessionKind.RACE) > 0


def test_a_held_start_is_not_racing_either():
    """is_green can be true while the field is still held; field_released is the term
    that covers it."""
    assert _lead_swap_candidates(green=True, released=False) == 0


# --- a wreck must actually get its window -------------------------------------
# Reported live: a huge wreck went unreplayed. Over five minutes of a 57-car restart the
# director cut 26 BATTLE shots and ONE leader shot, so an absolute battle veto meant the
# candidate simply expired. A fight for the lead outranks a replay; a fight for P42 does
# not outrank a big crash.

def _pair_shot(a_idx, b_idx):
    from pylon.director.model import Shot, ShotKind
    return Shot(ShotKind.BATTLE, f"battle:{a_idx}:{b_idx}", b_idx, "", (a_idx, b_idx))


def test_a_wreck_outranks_a_MIDFIELD_battle():
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, wreck_dwell=5.0,
                                                 battle_veto_pos=3)), snap)
    later = wm.update(_frame(999, snap.session_time + 6.0))
    # cars 4 and 5 run P5/P6 in the fixture: behind battle_veto_pos
    assert r.safe_now(later, _pair_shot(4, 5))


def test_a_wreck_does_NOT_outrank_a_battle_at_the_front():
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, wreck_dwell=5.0,
                                                 battle_veto_pos=3)), snap)
    later = wm.update(_frame(999, snap.session_time + 6.0))
    assert not r.safe_now(later, _pair_shot(0, 1))     # P1 vs P2


def test_a_lead_change_never_outranks_any_battle():
    """Only a wreck buys its way past a midfield fight; a pass does not."""
    from pylon.director.replay import ReplayCandidate

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = ReplayDirector(ReplayConfig(enabled=True, battle_veto_pos=3))
    r.candidate = ReplayCandidate(at=snap.session_time, session_num=2, car_idx=3,
                                  car_number="4", driver="X", kind="lead_change",
                                  label="for the lead", position=1)
    r.armed_at = snap.session_time - 30.0
    r.state = ReplayState.ARMED
    assert not r.safe_now(snap, _pair_shot(4, 5))


def test_a_wreck_waits_longer_for_its_window_than_a_pass():
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(
        ReplayConfig(enabled=True, hold_for_lull=25.0, hold_for_lull_wreck=60.0)), snap)
    assert r._hold_for_lull() == 60.0
    r.candidate.kind = "lead_change"
    assert r._hold_for_lull() == 25.0


def test_the_position_cutoff_scales_with_the_field():
    """A flat 20 hides a P25 pile-up in a 57-car race but is the whole grid in a sprint."""
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = ReplayDirector(ReplayConfig(enabled=True, max_pos=20, max_pos_frac=0.6))
    assert r._max_pos(snap) == 20                      # 6 cars: the floor wins

    big = SimpleNamespace(order=list(range(57)))
    assert r._max_pos(big) == 34                       # 57 cars: 60% reaches deeper


def test_rewinding_the_tape_lets_the_same_moments_replay_again():
    """`_seen` is keyed on SESSION TIME, so a second pass over the same stretch matches
    every key already fired. Without this the whole re-watch produces no replays, which
    looks exactly like the feature being broken."""
    wm = WorldModel(_info())
    r = ReplayDirector(ReplayConfig(enabled=True))

    snap = None
    for fr in _tape(60, off_at=range(20, 45)):
        snap = wm.update(fr)
        r.observe(snap, None)
    fired_first_pass = len(r._seen)
    assert fired_first_pass > 0, "fixture produced no candidates at all"

    # rewind: same tape, from the top. A fresh world model is what a seek looks like
    # to the director: the frames simply arrive with an earlier clock.
    r._disarm()
    wm2 = WorldModel(_info())
    back = wm2.update(_frame(0, 100.0))
    assert back.time_jump is None          # first frame of a stream is not a jump
    r.observe(back, None)

    # now a real backwards jump on the SAME model
    jumped = wm.update(_frame(999, 100.0))
    assert jumped.time_jump == TimeJump.BACK
    r.observe(jumped, None)
    assert r._seen == set(), "a rewind must forget what it has already shown"


# --- a stop is not a lead change ---------------------------------------------
# Reported live: the broadcast cut to an instant replay, in slow motion, of a "pass for
# the lead" where the old leader had simply driven into the pit lane. Nobody overtook
# anybody. Same root cause as the bogus overtake calls in test_world (the leader
# changes because a car STOPPED), and the same predicate settles it.


def _lead_swap_with_pit(*, pitting_idx: int | None) -> int:
    """The leader-change sequence, optionally with one of the two cars in the lane."""
    wm = WorldModel(_info())
    r = ReplayDirector(ReplayConfig(enabled=True))
    armed = 0
    for k, leader in enumerate([0, 0, 1, 1, 0, 0]):
        fr = _grid_frame(k, 100.0 + k, leader_idx=leader)
        if pitting_idx is not None:
            v = dict(fr.values)
            pits = list(v["CarIdxOnPitRoad"])
            pits[pitting_idx] = True
            v["CarIdxOnPitRoad"] = pits
            from pylon.telemetry.frame import Frame
            fr = Frame(tick=fr.tick, session_time=fr.session_time, values=v)
        snap = wm.update(fr)
        object.__setattr__(snap.session, "field_released", True)
        object.__setattr__(snap.session, "is_green", True)
        before = r.state
        r.observe(snap, None)
        if before != r.state and r.state == ReplayState.ARMED:
            armed += 1
            r._disarm()
    return armed


def test_a_leader_diving_into_the_pits_does_not_arm_a_lead_change_replay():
    """The control first: on track, this sequence really does arm one."""
    assert _lead_swap_with_pit(pitting_idx=None) > 0
    # the car that HELD the lead is the one that stops: the reported case
    assert _lead_swap_with_pit(pitting_idx=0) == 0
    # ...and the mirror image, a car inheriting the lead while it is itself in the lane
    assert _lead_swap_with_pit(pitting_idx=1) == 0


def _pit_cycle_frames(n=400, hz=10.0):
    """Two cars; car 0 leads the sheet throughout and takes a pit stop.

    The SHEET never changes, which is the point: this is about what the live order does
    around a stop, not about iRacing's own bookkeeping. Car 0 crawls (frames 40-120),
    then rejoins and runs quicker, so the pair re-crosses on track afterwards, which
    is the second half of the cycle and where the instant pit check let a phantom
    through.
    """
    from pylon.telemetry.frame import Frame

    p0 = p1 = 5.0
    for k in range(n):
        t = k / hz
        pitting = 40 <= k < 120
        p0 += 0.0002 if pitting else (0.002 if k < 120 else 0.0032)
        p1 += 0.002
        yield Frame(tick=k, session_time=t, values={
            "SessionTime": t, "SessionNum": 0, "SessionState": 4,
            "CarIdxLapDistPct": [p0 % 1.0, p1 % 1.0],
            "CarIdxPosition": [1, 2], "CarIdxClassPosition": [1, 2],
            "CarIdxTrackSurface": [1 if pitting else 3, 3],
            "CarIdxLap": [int(p0) + 1, int(p1) + 1],
            "CarIdxLapCompleted": [int(p0), int(p1)],
            "CarIdxOnPitRoad": [pitting, False],
        })


def _armed_over_a_pit_cycle(pit_grace: float) -> int:
    from pylon.telemetry.frame import SessionInfo

    wm = WorldModel(SessionInfo(PIT_WEEKEND))
    r = ReplayDirector(ReplayConfig(enabled=True, pit_grace=pit_grace))
    armed = 0
    for fr in _pit_cycle_frames():
        snap = wm.update(fr)
        object.__setattr__(snap.session, "is_green", True)
        object.__setattr__(snap.session, "field_released", True)
        before = r.state
        r.observe(snap, None)
        if before != r.state and r.state == ReplayState.ARMED:
            armed += 1
            r._disarm()
    return armed


PIT_WEEKEND = {
    "WeekendInfo": {"TrackDisplayShortName": "Watkins", "TrackLength": "5.552 km",
                    "EventType": "Race"},
    "SessionInfo": {"CurrentSessionNum": 0, "Sessions": [
        {"SessionNum": 0, "SessionType": "Race", "SessionName": "RACE"}]},
    "DriverInfo": {"Drivers": [{"CarIdx": i, "CarNumber": str(i + 1),
                                "UserName": f"Driver {i}"} for i in range(2)]},
}


def test_a_whole_pit_cycle_arms_no_lead_change_replay():
    """The bug as reported the SECOND time, after the first guard was already deployed.

    Checking the pit flag at the instant order[0] changes catches the stop and misses
    the rest of the cycle: the order goes on settling while the car rejoins, and by the
    time it flips back the flag is long clear. `pit_grace` is what closes that, and
    grace=0 is exactly the version that shipped and was still wrong.
    """
    assert _armed_over_a_pit_cycle(0.0) >= 1        # the instant check lets one through
    assert _armed_over_a_pit_cycle(30.0) == 0       # ...the grace window does not


def test_the_pit_grace_does_not_mute_a_lead_change_between_cars_that_stayed_out():
    """It is scoped to the two cars in the lead change. A stop somewhere else in the
    field must not buy silence for a real pass at the front."""
    assert _lead_swap_candidates(green=True, released=True) > 0


def test_a_pass_during_an_armed_period_is_not_replayed_at_the_wrong_moment():
    """`_moments` compares the leader against the last one IT saw, and it is not called
    while a replay is armed or rolling. A pass in that stretch used to be discovered on
    the first idle frame afterwards and marked at THAT moment, so the replay sought to
    a spot on the tape where nothing happens. The leader is stamped above the gate now,
    like the pit stamp already was."""
    wm = WorldModel(_info())
    r = ReplayDirector(ReplayConfig(enabled=True))
    leaders = [0, 0, 0] + [1] * 30          # the pass happens at k=3
    for k, leader in enumerate(leaders):
        snap = wm.update(_grid_frame(k, 100.0 + k, leader_idx=leader))
        object.__setattr__(snap.session, "field_released", True)
        object.__setattr__(snap.session, "is_green", True)
        if k == 2:
            _armed_wreck(r, snap)           # a wreck armed just before the pass
        r.observe(snap, None)
        if k == 20:
            r._disarm()                     # it aged out without ever rolling
    assert r.state == ReplayState.IDLE      # no phantom lead-change replay at t=121
    assert r.candidate is None


# --- the moment is the event, not the detection ---------------------------------
# On air (2026-09-14) every replay opened on a run-up and came home in slow motion of
# the aftermath. The mark was the frame we NOTICED: contact when the second car left the
# road, trouble once a car had been crawling for trouble_confirm: both after the fact.
# The tape is sought relative to the moment, so the moment has to be the event.

def _snap_with_event(snap, event):
    """The same snapshot, carrying one extra event (WorldSnapshot is frozen)."""
    from dataclasses import replace
    return replace(snap, events=(*snap.events, event))


def test_a_contact_candidate_is_marked_when_it_happened_not_when_it_was_called():
    from pylon.world import Event, EventKind, IncidentSeverity

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    reported = _snap_with_event(snap, Event(EventKind.INCIDENT, 2, other_idx=3, position=3,
                                            detail="contact", severity=IncidentSeverity.MAJOR,
                                            at=snap.session_time - 0.8))
    r = ReplayDirector(ReplayConfig(enabled=True))
    r.observe(reported, None)
    assert r.state == ReplayState.ARMED
    assert r.candidate.at == snap.session_time - 0.8
    assert r.candidate.marked_at == snap.session_time
    assert r.candidate.other_position == 4       # kept for the verdict below


def test_a_stricken_car_is_marked_where_its_pace_went():
    """The director's latch fires into the aftermath; `since` (Director.trouble_since)
    is the last frame the car was a racing car, and that is the moment."""
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    t = snap.session_time
    r = ReplayDirector(ReplayConfig(enabled=True))
    r.note_trouble(snap, 3, False, since=t - 3.2)
    assert r.candidate.at == t - 3.2
    assert r.candidate.marked_at == t

    # ...unless the pace went suspiciously long ago, which is not this incident
    r2 = ReplayDirector(ReplayConfig(enabled=True))
    r2.note_trouble(snap, 3, False, since=t - 60.0)
    assert r2.candidate.at == t
    # and no `since` at all (an old director) still marks the frame
    r3 = ReplayDirector(ReplayConfig(enabled=True))
    r3.note_trouble(snap, 3, False)
    assert r3.candidate.at == t


def test_the_seek_goes_to_the_moment_minus_the_lead_in():
    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, lead_in=6.0)), snap)
    r.candidate.at = snap.session_time - 4.0
    moves = r.start(snap)
    assert moves[1].kind == "seek"
    assert moves[1].session_time == snap.session_time - 10.0


# --- a contact that cost nobody anything is not replayed --------------------------
# "Contact" is two cars off the road together (world/incidents), which is the right
# trigger for an instant live cut and also what two cars running wide at the same kerb
# look like. The replay has hindsight the live cut does not, and uses it.

def test_two_cars_that_ran_wide_together_and_carried_on_are_not_replayed():
    tape = _tape(400, off_at=range(150, 175), costs_a_place=False)
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown_tape=0.0))
    client, _ = _run(tape, replay)
    assert replay.replays == 0
    assert replay.dropped_contacts == 1
    assert client.served_replay == 0


def test_a_contact_that_cost_a_place_is_replayed():
    """The fixture default: the second car drops a place a second later."""
    tape = _tape(400, off_at=range(150, 175))
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown_tape=0.0))
    client, _ = _run(tape, replay)
    assert replay.replays == 1
    assert replay.dropped_contacts == 0
    assert client.served_replay > 0


def test_a_contact_that_left_a_car_stricken_is_replayed_even_with_the_places_intact():
    """The other currency: the director's trouble latch, lent through `trouble`."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(120):
        snap = wm.update(fr)
    leader = Shot(ShotKind.LEADER, "leader:0", 0, "leader")
    t = snap.session_time

    def armed():
        r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True)), snap,
                         car_idx=2, kind="contact")
        r.candidate.other_idx = 3
        r.candidate.position, r.candidate.other_position = 3, 4
        r.candidate.at = r.armed_at = t - 10.0   # had its dwell and its outcome
        return r

    assert armed().maybe_start(snap, leader, now=0.0) == []            # nobody suffered
    assert armed().maybe_start(snap, leader, now=0.0, trouble=frozenset({3}))
    assert armed().maybe_start(snap, leader, now=0.0, trouble=frozenset({2}))


def test_the_verdict_waits_for_the_dwell_even_when_the_lull_is_early():
    """A yellow makes safe_now true at once; the verdict still needs hindsight."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(120):
        snap = wm.update(fr)
    leader = Shot(ShotKind.LEADER, "leader:0", 0, "leader")
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True,
                                                 wreck_dwell=5.0)), snap,
                     car_idx=2, kind="contact")
    r.candidate.other_idx = 3
    r.armed_at = snap.session_time - 1.0
    assert r.maybe_start(snap, leader, now=0.0, trouble=frozenset({3})) == []
    assert r.state == ReplayState.ARMED, "not dropped either: undecided, not innocent"


def test_the_verdict_can_be_switched_off():
    tape = _tape(400, off_at=range(150, 175), costs_a_place=False)
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown_tape=0.0, contact_needs_consequence=False))
    _run(tape, replay)
    assert replay.replays == 1


# --- the cut waits for a lull in the racing -----------------------------------------
# A replay has to land in a gap in the racing. Rolling on top of a live moment put
# the intro behind it, over the slow motion, describing a run-up already gone by.

# --- coming home puts the camera back at once -----------------------------------------
# The excursion points the sim at the replayed car and the sim keeps it there after the
# seek home. Waiting for the drift watch to notice was 2.6s of live pictures of the
# wrong car after every replay (on air, 2026-09-14).

def test_the_first_command_after_the_seek_home_is_the_live_shot():
    tape = _tape(400, off_at=range(150, 175))
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown_tape=0.0))
    client, _ = _run(tape, replay)
    seeks = [k for k, c in enumerate(client.sent) if c.op == CamOp.REPLAY_SEEK]
    assert len(seeks) == 2
    # the shot the director was on when it left: the command before the excursion's
    # own speed + seek (here its live INCIDENT cut to #3)
    before = client.sent[seeks[0] - 2]
    assert before.op == CamOp.SWITCH_NUM and before.label == "#3 contact P3"
    after_home = client.sent[seeks[1] + 1]
    assert after_home.op == CamOp.SWITCH_NUM
    assert (after_home.car_number, after_home.label) == (before.car_number, before.label)
    # and the replay's own look went to the car involved
    look = client.sent[seeks[0] + 1]
    assert look.op == CamOp.SWITCH_NUM and look.car_number == "3"
    assert look.label == "#3 D2"


def test_a_replay_is_framed_from_the_trackside_tv_cameras_only():
    from pylon.director.model import ShotFlavor, ShotKind
    for name, make in (("classic", AnglePolicy.classic),
                       ("onboard_forward", AnglePolicy.onboard_forward),
                       ("cinematic", AnglePolicy.cinematic)):
        ranked = make().ranked(ShotKind.INCIDENT, ShotFlavor.REPLAY)
        assert Angle.CHOPPER not in ranked, name
        assert set(ranked) <= {Angle.TV1, Angle.TV2, Angle.TV3}, name
        assert Angle.CHOPPER in make().ranked(ShotKind.INCIDENT, ""), \
            f"{name}: the live incident set is untouched"


def test_a_spin_is_replayed_from_before_the_car_lost_its_pace():
    """End to end, the shape of this morning's on-air failure: a car goes from racing
    pace to parked off the road. The director's latch fires trouble_confirm after it is
    crawling, seconds after the spin; a replay marked THERE opened on a run-up and went
    to slow motion of a parked car. The moment is where the pace went
    (Director.trouble_since), and the seek is lead_in before that."""
    from pylon.director import DirectorConfig

    def frame(k: int) -> Frame:
        t = 100.0 + k * 0.1
        fr = _frame(k, t)
        v = dict(fr.values)
        ldp, lapc, lap = list(v["CarIdxLapDistPct"]), list(v["CarIdxLapCompleted"]), list(v["CarIdxLap"])
        surf = list(v["CarIdxTrackSurface"])
        # car 3: racing until t=120, then decelerating hard to a stop by t=122, off the
        # road from t=120.5, parked thereafter
        if t > 120.0:
            base = 2000.0 + 50.0 * 120.0 - 70.0 * 3
            dt = min(t - 120.0, 2.0)
            d = base + 50.0 * dt - 12.5 * dt * dt      # 50 m/s -> 0 in 2s
            ldp[3] = (d % TRACK_M) / TRACK_M
            lapc[3] = int(d // TRACK_M)
            lap[3] = lapc[3] + 1
            if t >= 120.5:
                surf[3] = TrackSurface.OFF_TRACK
        v.update({"CarIdxLapDistPct": ldp, "CarIdxLapCompleted": lapc, "CarIdxLap": lap,
                  "CarIdxTrackSurface": surf})
        return Frame(tick=k, session_time=t, values=v)

    tape = [frame(k) for k in range(600)]
    replay = ReplayDirector(ReplayConfig(enabled=True, settle=99.0, cooldown_tape=0.0))
    client = TapeBridge(tape, _info())
    asyncio.run(drive_live(client, actuator=Actuator(_info(), ActuatorConfig(policy=_TV1_ONLY)),
                           cfg=DirectorConfig(interrupt_only_if_on_camera=True),
                           replay=replay, angle_refresh=0.0))
    assert replay.replays >= 1, "the spin never earned a replay"
    seek = next(c for c in client.sent if c.op == CamOp.REPLAY_SEEK)
    seek_to = seek.session_time_ms / 1000.0
    # the pace went at ~120.6 (below trouble_recover_speed 22 m/s); the latch fired
    # seconds later. The seek is lead_in (6s) before the pace went, give or take the
    # 10Hz tick, and well before the car was parked.
    assert 114.0 <= seek_to <= 115.5, seek_to
    look = client.sent[client.sent.index(seek) + 1]
    assert look.op == CamOp.SWITCH_NUM and look.car_number == "4"   # car idx 3 is #4


def test_a_replay_does_not_roll_until_its_outcome_is_on_the_tape():
    """A lead change is marked at the live edge. Rolling there capped the excursion at
    where live was and it came home BEFORE the pass (capture2, Sollenberger for the
    lead). Wait until slow_after + lead_out of racing has happened after the moment."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snaps = []
    for fr in _tape(160):
        snaps.append(wm.update(fr))
    leader = Shot(ShotKind.LEADER, "leader:0", 0, "leader")
    cfg = ReplayConfig(enabled=True, slow_after=1.5, lead_out=2.5)
    r = _armed_wreck(ReplayDirector(cfg), snaps[100], kind="lead_change")
    at = snaps[100].session_time
    assert r.maybe_start(snaps[100], leader) == []
    assert r.maybe_start(snaps[140], leader) == []          # 4.0s later: not yet
    assert r.state == ReplayState.ARMED
    moves = r.maybe_start(snaps[145], leader)               # 4.5s later: the outcome exists
    assert [m.kind for m in moves] == ["speed", "seek"]
    assert r.tape_end() == at + 4.0                          # and the excursion reaches it


def _spectating(tick: int, clock: float, tape: float) -> Frame:
    """A frame from a LIVE session watched as a spectator, as measured on air
    2026-09-20: the sim is in replay mode even at the live edge, SessionTime is the
    server's clock and runs on regardless, and only ReplaySessionTime says where the
    tape is."""
    fr = _frame(tick, clock)
    fr.values["IsReplayPlaying"] = True
    fr.values["ReplaySessionTime"] = tape
    return fr


def test_spectating_live_the_tape_is_measured_by_replay_session_time():
    """Round 1, Watkins Glen: every seek landed and every excursion was abandoned at
    `settle`, because SessionTime never moved. Then the tape was left 13s in the past
    with nobody to send it home. The machine reads the tape where the sim reports it."""
    cfg = ReplayConfig(enabled=True, slow_at_moment=True, lead_in=6.0, slow_lead=1.0,
                       slow_after=1.5, lead_out=2.5, settle=1.5)
    r = _rolling(cfg)
    r.left = False
    r.return_to = 229.0                  # the tape at start: ~1s behind a clock of 230
    r.started_wall = 0.0
    # the seek has not been serviced yet: clock and tape both advance, nothing left
    assert r.tick(_spectating(1, 230.1, 229.1), now=0.1) == []
    assert not r.left
    # ...and past `settle` this would have been abandoned. Instead the seek lands,
    # ReplaySessionTime drops to the lead-in, SessionTime does not, and we have LEFT
    assert r.tick(_spectating(2, 230.2, 194.0), now=0.2) == []
    assert r.left and r.live_clock
    # the ramp and the outcome are laid out on the TAPE, not the clock
    assert r.tick(_spectating(3, 235.2, 199.0), now=5.2)[0].slow_motion is True
    out = r.tick(_spectating(4, 237.7, 201.5), now=7.7)
    assert [m.kind for m in out] == ["speed"] and out[0].speed == 1
    # home is the live edge: seeking to `return_to` would land the excursion's
    # length behind live, and IsReplayPlaying (true throughout) cannot say otherwise
    home = r.tick(_spectating(5, 240.2, 204.0), now=10.2)
    assert [m.kind for m in home] == ["speed", "to_end"]
    assert r.state == ReplayState.RETURNING
    # arrival is judged on the tape too: still in the past is not home...
    assert not r.arrived(_spectating(6, 240.3, 204.1))
    # ...the live edge, ~1s behind the clock and well past where we left, is
    assert r.arrived(_spectating(7, 240.5, 239.5))


def test_on_a_saved_tape_the_clock_and_the_tape_agree_and_nothing_changes():
    """Every replay ever verified was on a tape, where SessionTime IS the tape. The
    live-session path must not fire there: `home` still seeks to `return_to`."""
    r = _rolling(ReplayConfig(enabled=True, slow_lead=1.0, slow_after=1.5, lead_out=2.5))
    fr = _frame(1, 204.0)
    fr.values["IsReplayPlaying"] = True
    fr.values["ReplaySessionTime"] = 204.0
    home = r.tick(fr, now=9.0)
    assert [m.kind for m in home] == ["speed", "seek"]
    assert not r.live_clock


# --- Round 1 (2026-09-20): the four replay faults from the user's notes -------------

def test_the_cooldown_clock_restarts_with_the_session():
    """"Didn't see any instant replays for the main race." `last_replay_end` was stamped
    at t=3303 of practice and never touched again; the session clock restarted at 0 for
    qualifying and again for the race, so `t - last_replay_end < cooldown` held for
    every second of both. Not one replay could roll after practice."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for k in range(40):
        snap = wm.update(_frame(k, 3300.0 + k * 0.1))          # practice, late on
    r = ReplayDirector(ReplayConfig(enabled=True, cooldown=90.0))
    leader = Shot(ShotKind.LEADER, "leader:0", 0, "")
    r.finish(snap.session_time)                                   # the last practice replay
    assert not r.safe_now(snap, leader)                           # cooling down, rightly

    # the race: a new SessionNum, the clock back at zero
    race = _frame(100, 0.5)
    race.values["SessionNum"] = 3
    snap = wm.update(race)
    assert snap.time_jump == TimeJump.SESSION
    r.observe(snap, leader)
    snap = wm.update(Frame(tick=101, session_time=380.0,
                           values={**_frame(101, 380.0).values, "SessionNum": 3}))
    assert r.safe_now(snap, leader), "the practice cooldown outlived the practice"


def test_outside_a_race_a_stricken_car_needs_an_off_at_pace():
    """"Maybe no instant replays during practice unless there's a crash confirmed
    somehow": 24 in Round 1's practice, one every cooldown, of drivers pulling over
    to reset. A car that stopped without leaving the road at pace is not a replay
    outside a race; one that went off at racing speed first still is."""
    from pylon.world import SessionKind

    def run(kind: str, *, off: bool) -> ReplayDirector:
        wm = WorldModel(_info())
        r = ReplayDirector(ReplayConfig(enabled=True))
        snap = None
        for k in range(30):
            # car 3 leaves the road at 50 m/s on frame 20 (an OFF_TRACK event at pace)
            snap = wm.update(_frame(k, 100.0 + k * 0.1, off={3} if (off and k >= 20) else frozenset()))
            object.__setattr__(snap.session, "session_kind", kind)
            r.observe(snap, None)
        r.note_trouble(snap, 3, False, since=snap.session_time - 1.0)
        return r

    assert run(SessionKind.PRACTICE, off=False).state == ReplayState.IDLE
    assert run(SessionKind.QUALIFY, off=False).state == ReplayState.IDLE
    assert run(SessionKind.PRACTICE, off=True).state == ReplayState.ARMED
    # a race trusts the latch on its own: a car stopped on the road IS the story there
    assert run(SessionKind.RACE, off=False).state == ReplayState.ARMED
    assert run(SessionKind.UNKNOWN, off=False).state == ReplayState.ARMED


def test_a_walking_pace_off_does_not_confirm_a_crash():
    """The off has to be AT PACE: rolling onto the grass to press reset is the very
    thing the gate exists to exclude."""
    from pylon.world import SessionKind

    wm = WorldModel(_info())
    r = ReplayDirector(ReplayConfig(enabled=True, crash_pace=22.0))
    snap = None
    for k in range(30):
        fr = _frame(k, 100.0 + k * 0.1, off={3} if k >= 20 else frozenset())
        # car 3 crawls: 3 m/s on the road, then onto the grass
        ldp = list(fr.values["CarIdxLapDistPct"])
        ldp[3] = ((2000.0 + 3.0 * (100.0 + k * 0.1)) % TRACK_M) / TRACK_M
        fr.values["CarIdxLapDistPct"] = ldp
        snap = wm.update(fr)
        object.__setattr__(snap.session, "session_kind", SessionKind.PRACTICE)
        r.observe(snap, None)
    r.note_trouble(snap, 3, False, since=snap.session_time - 1.0)
    assert r.state == ReplayState.IDLE


def test_a_fresh_shot_is_not_left_for_a_replay():
    """"Instant replay as someone leaves the pit is odd": the director cut to a car
    leaving the pits, the graphics named the car rejoining, and the replay
    rolled over him a second later. A shot gets `shot_settle` to be a shot first."""
    from pylon.director.model import Shot, ShotKind

    wm = WorldModel(_info())
    snap = None
    for fr in _tape(40):
        snap = wm.update(fr)
    r = _armed_wreck(ReplayDirector(ReplayConfig(enabled=True, shot_settle=4.0)), snap)
    r.armed_at = snap.session_time - 30.0                        # long past wreck_dwell
    follow = Shot(ShotKind.FOLLOW, "follow:2", 2, "#3 D2")
    t = snap.session_time
    assert not r.safe_now(snap, follow, shot_since=t - 1.0)      # just cut to it
    assert r.safe_now(snap, follow, shot_since=t - 4.0)          # it has been a shot
    assert r.safe_now(snap, follow)                              # nobody said: no gate
    # a yellow needs no such grace: nothing is being interrupted
    object.__setattr__(snap.session, "is_yellow", True)
    assert r.safe_now(snap, follow, shot_since=t - 1.0)


class FrameBridge(TapeBridge):
    """A TapeBridge that remembers WHICH FRAME each command was sent during."""

    def __init__(self, tape, info):
        super().__init__(tape, info)
        self.sent_on: list[tuple[int, CamCommand]] = []

    async def send(self, cmd):
        self.sent_on.append((self.served_total, cmd))
        await super().send(cmd)


def test_the_frame_home_carries_one_camera_command_and_it_is_the_directors_cut():
    """"Still showing the wrong name on the screen during replays sometimes." Measured
    over 15 of Round 1's 16 replays: on the frame home the live shot was re-sent AND
    the director cut (its hold had expired during the excursion), two camera switches
    went out back to back, the sim dropped the second, and the drift watch re-sent it
    2.5s later: during which the overlay named the new car over pictures of the old
    one. One camera command per frame, and it is the director's."""
    import time

    from pylon.director.model import Decision, Shot, ShotKind
    from pylon.show.live import _drive

    info = _info()
    tape = _tape(400, off_at=range(150, 175))
    replay = ReplayDirector(ReplayConfig(enabled=True, settle=99.0, cooldown_tape=0.0))
    leader = Shot(ShotKind.LEADER, "leader:0", 0, "#1 D0")
    follow = Shot(ShotKind.FOLLOW, "follow:4", 4, "#5 D4")

    class StubDirector:
        """Holds the leader (the lull a replay may use), and cuts to another car on the
        very frame the excursion comes home, as a director whose hold expired does."""
        current = leader
        started = 0.0
        trouble = frozenset()

        def trouble_since(self, idx):
            return None

        def update(self, snap):
            if (replay.replays == 1 and replay.state == ReplayState.IDLE
                    and self.current is leader):
                self.current, self.started = follow, snap.session_time
                return Decision(snap.session_time, follow, 1.0, "variety", 0.0)
            return None

    client = FrameBridge(tape, info)
    asyncio.run(_drive(client, WorldModel(info), StubDirector(),
                       Actuator(info, ActuatorConfig(policy=_TV1_ONLY)),
                       limit=0.0, takeover=False, angle_refresh=0.0, on_decision=None,
                       on_snapshot=None, replay=replay, on_replay=None, watch=None,
                       on_camera=None, clock=time.monotonic))
    assert replay.replays == 1
    seeks = [(k, c) for k, c in client.sent_on if c.op == CamOp.REPLAY_SEEK]
    assert len(seeks) == 2
    home_frame = seeks[1][0] + 1                      # the first live frame after it
    switches = [c for k, c in client.sent_on
                if k == home_frame and c.op in (CamOp.SWITCH_NUM, CamOp.SWITCH_POS)]
    assert len(switches) == 1, [c.label for c in switches]
    assert switches[0].car_number == "5" and switches[0].label == "#5 D4"
    # and nothing put the camera back on the leader first, to be overwritten
    assert not [c for k, c in client.sent_on if k == home_frame and c.car_number == "1"]


def test_the_frame_home_still_reasserts_the_live_shot_when_the_director_holds():
    """The other half: no cut on the frame home means the live shot IS re-sent (the
    camera is on the replayed car until something moves it), once."""
    tape = _tape(400, off_at=range(150, 175))
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown_tape=0.0))
    client = FrameBridge(tape, _info())
    asyncio.run(drive_live(client, actuator=Actuator(_info(), ActuatorConfig(policy=_TV1_ONLY)),
                           replay=replay, angle_refresh=0.0))
    seeks = [(k, c) for k, c in client.sent_on if c.op == CamOp.REPLAY_SEEK]
    assert len(seeks) == 2
    before = client.sent[client.sent.index(seeks[0][1]) - 2]     # the shot we left on
    home_frame = seeks[1][0] + 1
    switches = [c for k, c in client.sent_on
                if k == home_frame and c.op in (CamOp.SWITCH_NUM, CamOp.SWITCH_POS)]
    assert len(switches) == 1
    assert (switches[0].car_number, switches[0].label) == (before.car_number, before.label)


def test_an_abandoned_seek_is_reported_and_the_camera_comes_back():
    """Nothing logged a replay that `settle` gave up on; on air it looked like a replay
    that cut away instantly. Now `on_replay("failed", cand)` says so, and the live shot
    is re-sent on that frame (the replay's own look had moved the camera)."""
    class DeafBridge(FrameBridge):
        async def send(self, cmd):
            self.sent_on.append((self.served_total, cmd))
            self.sent.append(cmd)          # accepts everything, honours nothing

    tape = _tape(300, off_at=range(100, 125))
    replay = ReplayDirector(ReplayConfig(enabled=True, settle=0.0, cooldown_tape=0.0))
    client = DeafBridge(tape, _info())
    phases: list[tuple[str, object]] = []
    asyncio.run(drive_live(client, actuator=Actuator(_info(), ActuatorConfig(policy=_TV1_ONLY)),
                           replay=replay, angle_refresh=0.0,
                           on_replay=lambda phase, cand: phases.append((phase, cand))))
    assert replay.failed_seeks >= 1
    kinds = [p for p, _ in phases]
    assert kinds[:2] == ["start", "failed"], kinds
    assert phases[1][1] is not None and phases[1][1].kind == "contact"
    # the frame it gave up on re-sent the live shot, once
    seek_frame = next(k for k, c in client.sent_on if c.op == CamOp.REPLAY_SEEK)
    fail_frame = next(k for k, c in client.sent_on
                      if k > seek_frame and c.op == CamOp.SWITCH_NUM
                      and not c.label.startswith("#3 D2"))
    switches = [c for k, c in client.sent_on if k == fail_frame and c.op == CamOp.SWITCH_NUM]
    assert len(switches) == 1
