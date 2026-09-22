"""Closing the camera loop: does the picture match the shot? (#68)

The camera pipeline is open loop everywhere else. The director decides a Shot, the
actuator turns it into a command, the controller sends it, and until now nothing
ever looked to see where the camera ended up, so every consumer downstream
rendered the director's INTENT and assumed it had become reality.

At Oulton Park on 2026-08-02 it had not: `CamCarIdx` read 5 while the shot was car
2, and the tower and the pop-ins described the car that was not on screen for a long
stretch, with no log line and no badge anywhere. Restarting the director fixed it
instantly, which is how we know the commands do reach the sim.

These tests are the four things that incident needs to never happen silently again,
and one that is really about a DIFFERENT bug: absent must not read as car zero. That
is the shape of #62, where folding a missing `IsReplayPlaying` into False badged
every live race REPLAY, and it is the single easiest way to make this feature worse
than not having it: a broadcast where the camera is re-asserted every few seconds
because the feed never said where it was.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import capture_or_skip

from pylon.camera.watch import DEBOUNCE, CameraWatch, subject_cars
from pylon.director.model import Shot, ShotKind
from pylon.telemetry.frame import Frame
from pylon.world import CameraView
from pylon.world import run as run_world
from pylon.world.builder import _camera

LEADER = Shot(ShotKind.LEADER, "leader:2", 2, "#9 Moreno")
BATTLE = Shot(ShotKind.BATTLE, "battle:12:15", 15, "#12 v #15", pair=(12, 15))


class _Snap:
    """Just enough WorldSnapshot for the watch: it reads three attributes."""

    def __init__(self, t: float, camera: CameraView | None, time_jump: str | None = None):
        self.session_time = t
        self.camera = camera
        self.time_jump = time_jump


def _on(car: int | None, group: int | None = 9) -> CameraView:
    return CameraView(car_idx=car, group=group)


# --- the reconcile path -----------------------------------------------------


def test_a_drifted_camera_is_re_asserted_after_the_debounce_and_not_before():
    """The headline. The camera legitimately lags a command by a frame or two, so a
    single disagreeing frame means nothing; a sustained one means the broadcast is
    showing the wrong car."""
    w = CameraWatch(debounce=2.5)
    # First disagreeing frame only starts the clock.
    assert w.observe(_Snap(100.0, _on(5)), LEADER) is None
    assert w.drifting
    # ...and nothing fires while it is still inside the debounce.
    for t in (100.5, 101.0, 102.0, 102.4):
        assert w.observe(_Snap(t, _on(5)), LEADER) is None, t
    drift = w.observe(_Snap(102.5, _on(5)), LEADER)
    assert drift is not None
    assert (drift.wanted, drift.actual) == (2, 5)
    assert drift.held == pytest.approx(2.5)
    assert drift.at == pytest.approx(102.5)
    assert drift.shot is LEADER
    # The Oulton Park numbers, in the log line an operator would have needed.
    assert "car 2" in str(drift) and "car 5" in str(drift)


def test_a_camera_that_stays_away_is_re_asserted_once_per_debounce_not_every_frame():
    """A re-assert restarts the clock. Otherwise a camera held elsewhere by something
    else would produce a command on every single frame: ten a second, forever."""
    w = CameraWatch(debounce=2.0)
    fired = [t for t in [x / 10 for x in range(200)]
             if w.observe(_Snap(t, _on(5)), LEADER) is not None]
    # Twenty seconds at 10Hz with the camera never coming back: one every two seconds.
    assert fired == pytest.approx([2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0])


def test_the_camera_coming_back_on_its_own_ends_the_drift():
    w = CameraWatch(debounce=2.5)
    w.observe(_Snap(10.0, _on(5)), LEADER)
    assert w.drifting
    assert w.observe(_Snap(11.0, _on(2)), LEADER) is None
    assert not w.drifting
    # ...and the clock really did restart: the debounce runs again from here.
    assert w.observe(_Snap(12.0, _on(5)), LEADER) is None
    assert w.observe(_Snap(13.0, _on(5)), LEADER) is None
    assert w.observe(_Snap(14.6, _on(5)), LEADER) is not None


# --- what does NOT count as disagreement ------------------------------------


def test_a_feed_with_no_camera_channels_reconciles_nothing():
    """Every existing recording, the synthetic source, and the two-box live path
    until the sim-box agent is rebuilt. Not knowing where the camera is must send no
    commands at all, forever."""
    w = CameraWatch(debounce=0.5)
    for t in range(100):
        assert w.observe(_Snap(float(t), None), LEADER) is None
    assert not w.drifting
    assert w.view is None


def test_an_absent_channel_is_not_the_camera_being_on_car_zero():
    """The #62 shape. `CamCarIdx` missing and `CamCarIdx` reading 0 are completely
    different facts, and car 0 is a real and very ordinary car, so a shot on car 2
    must drift against a reported 0 and must NOT drift against nothing reported."""
    absent = _camera(Frame(tick=1, session_time=1.0, values={}))
    assert absent is None

    zero = _camera(Frame(tick=1, session_time=1.0, values={"CamCarIdx": 0}))
    assert zero == CameraView(car_idx=0, group=None)

    w = CameraWatch(debounce=1.0)
    assert w.observe(_Snap(0.0, absent), LEADER) is None
    assert w.observe(_Snap(2.0, absent), LEADER) is None      # still nothing
    assert w.observe(_Snap(3.0, zero), LEADER) is None         # a real reading: clock starts
    assert w.observe(_Snap(4.5, zero), LEADER) is not None     # ...and it fires


def test_the_sims_own_no_car_sentinel_is_not_a_car_either():
    """`CamCarIdx` goes negative when the camera is not on a car. That is "no idea",
    the same answer as absent, and it is certainly not car index minus one."""
    view = _camera(Frame(tick=1, session_time=1.0,
                         values={"CamCarIdx": -1, "CamGroupNumber": 9}))
    assert view is not None and view.car_idx is None and view.group == 9

    w = CameraWatch(debounce=0.5)
    for t in range(20):
        assert w.observe(_Snap(float(t), view), LEADER) is None


def test_either_car_of_a_battle_pair_counts_as_the_shot_being_on_screen():
    """A battle names one car as the target and carries both. iRacing framing the
    other one is the shot working, not the camera drifting."""
    assert subject_cars(BATTLE) == frozenset({12, 15})
    assert subject_cars(LEADER) == frozenset({2})
    assert subject_cars(None) == frozenset()

    w = CameraWatch(debounce=0.5)
    for car in (15, 12, 15):
        assert w.observe(_Snap(0.0, _on(car)), BATTLE) is None
    assert not w.drifting
    # A third car is a real drift.
    w.observe(_Snap(10.0, _on(3)), BATTLE)
    assert w.observe(_Snap(11.0, _on(3)), BATTLE) is not None


def test_no_shot_at_all_is_nothing_to_reconcile_against():
    w = CameraWatch(debounce=0.5)
    for t in range(20):
        assert w.observe(_Snap(float(t), _on(5)), None) is None


def test_a_discontinuous_clock_drops_the_drift_instead_of_measuring_across_it():
    """A replay seek can leave the start of a disagreement in the future, and then
    `t - since` never reaches the debounce again: the check would be dead for the
    rest of the broadcast. Same reseat the director does on a TimeJump."""
    w = CameraWatch(debounce=1.0)
    w.observe(_Snap(500.0, _on(5)), LEADER)
    assert w.drifting
    assert w.observe(_Snap(20.0, _on(5), time_jump="replay"), LEADER) is None
    assert not w.drifting
    # ...and it re-arms cleanly on the new clock rather than staying broken.
    assert w.observe(_Snap(21.0, _on(5)), LEADER) is None
    assert w.observe(_Snap(22.5, _on(5)), LEADER) is not None


def test_reset_forgets_a_drift_in_progress():
    """What the live loop calls when the tape hands the camera back."""
    w = CameraWatch(debounce=1.0)
    w.observe(_Snap(10.0, _on(5)), LEADER)
    assert w.drifting
    w.reset()
    assert not w.drifting
    assert w.observe(_Snap(10.5, _on(5)), LEADER) is None


def test_the_default_debounce_sits_between_a_frame_and_the_angle_refresh():
    """It has to be longer than the frame or two the camera takes to obey, and
    shorter than the 9s hold re-frame: otherwise the re-frame would be the thing
    that maybe fixed it, which is what we had."""
    assert 0.3 < DEBOUNCE < 9.0


# --- against real data ------------------------------------------------------


def test_the_camera_channels_are_read_off_a_real_capture():
    """This project's standing rule: verify a channel against a real capture, never
    from vars.txt. capture2 is a driving capture, so the camera sits on the player's
    own car (index 0) for all 4279 frames and the GROUP moves 10 -> 9 underneath it.

    Which is also the honest limit of what real data proves here: the channel is
    live, populated, and moves. No capture in this repository contains a broadcast
    where the camera diverged from a director's shot, so the reconcile logic above is
    verified against constructed frames and the semantics documented in #68.
    """
    from pylon.telemetry.playback import PlaybackSource

    src = PlaybackSource(capture_or_skip("capture2.jsonl.gz"))
    seen_cars, seen_groups, n = set(), set(), 0
    for frame in src.frames():
        view = _camera(frame)
        assert view is not None, "capture2 carries the camera channels"
        seen_cars.add(view.car_idx)
        seen_groups.add(view.group)
        n += 1
    assert n > 4000
    assert seen_cars == {0}, seen_cars
    assert seen_groups == {9, 10}, seen_groups

    # ...and the whole snapshot really does carry it, not just the helper.
    src = PlaybackSource(capture_or_skip("capture2.jsonl.gz"))
    snap = next(run_world(src))
    assert snap.camera is not None and snap.camera.car_idx == 0


def test_the_synthetic_source_carries_no_camera_channels_so_the_dev_loop_is_quiet():
    """The premise every other test in this file rests on, asserted rather than
    assumed: the dev loop must behave exactly as it did before this feature."""
    from pylon.telemetry import SyntheticSource

    src = SyntheticSource(num_cars=8, duration_s=10, hz=10, seed=1)
    assert all(snap.camera is None for snap in run_world(src))


# --- through the live loop --------------------------------------------------
#
# Built on test_replay's tape harness: a fake bridge whose sim honours a seek, so a
# replay excursion is a real one and no socket is involved. The tapes here get a
# `CamCarIdx` written into every frame, which no source in this repository produces
#: the synthetic one carries no camera channels at all, and the two real captures
# are driving captures where the camera never left the player's car.


def _drifting(tape, on_car: int = 40):
    """That tape, with the sim reporting its camera parked on one car forever.

    Car 40 is a legitimate iRacing car index and is not in this roster of six, so it
    can never be a shot target: every frame is a genuine disagreement, which is the
    Oulton Park state held indefinitely.
    """
    return [
        Frame(tick=f.tick, session_time=f.session_time,
              values={**f.values, "CamCarIdx": on_car, "CamGroupNumber": 11})
        for f in tape
    ]


def _drive_tape(tape, *, camera_debounce, replay=None):
    """Run the live loop over a tape and report what reached the wire."""
    from test_replay import _TV1_ONLY, TapeBridge, _info

    from pylon.camera import Actuator, ActuatorConfig
    from pylon.show.live import drive_live

    drifts: list = []
    client = TapeBridge(tape, _info())
    n = asyncio.run(drive_live(
        client, actuator=Actuator(_info(), ActuatorConfig(policy=_TV1_ONLY)),
        replay=replay, angle_refresh=0.0, refresh_every=0.0,
        camera_debounce=camera_debounce,
        on_camera=lambda drift, cmd: drifts.append((drift, cmd)),
    ))
    return client, n, drifts


def test_a_drifted_camera_reaches_the_wire_as_a_re_asserted_command():
    """End to end: the sim says the camera is somewhere the director never asked for,
    and the command goes out again. This is the whole point of #68: at Oulton Park
    the only cure available to the operator was restarting the director."""
    from test_replay import _tape

    from pylon.camera import CamOp

    tape = _tape(400)
    _client, n, drifts = _drive_tape(_drifting(tape), camera_debounce=2.5)

    assert drifts, "a camera pinned to a car outside the field never reported a drift"
    # 40 seconds of tape at 10Hz with the camera never coming back: one re-assert per
    # debounce, and every one of them names the shot that is not on screen.
    assert len(drifts) >= 10
    for drift, cmd in drifts:
        assert drift.actual == 40
        assert drift.wanted == drift.shot.target_idx
        assert cmd.op == CamOp.SWITCH_NUM
        assert drift.held >= 2.5
    assert n > len(drifts)          # the cuts, plus these


def test_a_feed_with_no_camera_channel_sends_exactly_what_it_sent_before():
    """The strongest form of "absent changes nothing": the same tape without the
    channel produces byte-identical output to the feature being switched off."""
    from test_replay import _tape

    tape = _tape(400)
    with_check, n_check, drifts = _drive_tape(tape, camera_debounce=2.5)
    without, n_off, _ = _drive_tape(tape, camera_debounce=0.0)

    assert not drifts
    assert n_check == n_off
    assert [(c.op, c.car_number, c.group) for c in with_check.sent] \
        == [(c.op, c.car_number, c.group) for c in without.sent]


def test_nothing_is_re_asserted_while_the_tape_is_rolling():
    """#18's replay isolation, from the other side. During an excursion the camera is
    deliberately somewhere else, and a reconciler that did not know it would fight the
    replay for the camera and drag the picture back to the live race mid-highlight."""
    from test_replay import _tape

    from pylon.camera import CamOp
    from pylon.director import ReplayDirector
    from pylon.director.replay import ReplayConfig

    tape = _drifting(_tape(400, off_at=range(150, 175)))
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown=0.0, cooldown_tape=0.0))
    client, _n, drifts = _drive_tape(tape, camera_debounce=2.5, replay=replay)

    assert replay.replays >= 1, "no replay ever fired, so nothing was proven"
    seeks = [k for k, c in enumerate(client.sent) if c.op == CamOp.REPLAY_SEEK]
    assert len(seeks) >= 2, "expected a seek away and a seek home"
    away, home = seeks[0], seeks[1]

    # The only car-pointing command allowed between the two seeks is the deliberate
    # one the replay itself sends to frame the car involved.
    between = [c for c in client.sent[away:home] if c.op == CamOp.SWITCH_NUM]
    assert len(between) <= 1, [str(c) for c in between]
    # ...and no drift came off a REPLAYED frame. The excursion serves earlier session
    # times, so a drift measured on one would land out of order; every drift arriving
    # on a strictly rising clock means every one of them was live.
    times = [d.at for d, _c in drifts]
    assert times == sorted(times), times
    assert len(set(times)) == len(times), "a session time was directed twice"


def test_the_watch_is_reset_when_the_tape_hands_the_camera_back():
    """The frame the loop returns on is live again and gets directed like any other.
    Without the reset it would compare that shot against wherever the tape left the
    camera and re-assert instantly, breaking the isolation from the far side."""
    from test_replay import _tape

    from pylon.director import ReplayDirector
    from pylon.director.replay import ReplayConfig

    tape = _drifting(_tape(400, off_at=range(150, 175)))
    replay = ReplayDirector(ReplayConfig(enabled=True, roll=0.0, settle=99.0,
                                         cooldown=0.0, cooldown_tape=0.0))
    _client, _n, drifts = _drive_tape(tape, camera_debounce=2.5, replay=replay)

    assert replay.replays >= 1
    # Every reported drift was measured over a full debounce of LIVE frames; none was
    # measured across the excursion, which would show up as an inflated hold.
    assert drifts
    for drift, _cmd in drifts:
        assert drift.held < 10.0, f"a drift measured across the seek: {drift.held}s"
