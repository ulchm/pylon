"""Scene switching: SessionState -> which OBS scene is on air.

SceneDirector is pure, so it is tested directly with hand-built snapshots
rather than through a fake OBS. What matters here is the editorial behaviour:
it settles before cutting, it does not throw the finish away, and it says
nothing at all when it cannot tell what the session is doing.
"""

import pytest

from pylon.obs.scenes import ObsSwitcher, SceneConfig, SceneDirector
from pylon.obs.setup import PROGRAM_SCENE, SCENE_PREFIX

PRE = f"{SCENE_PREFIX}Starting Soon"
POST = f"{SCENE_PREFIX}Race Complete"
MID = f"{SCENE_PREFIX}Intermission"
from pylon.telemetry.constants import SessionState, TrackSurface
from pylon.world.model import CarState, SessionKind, SessionSnapshot, WorldSnapshot


def _car(idx):
    """A car sitting on the grid: in the world, on the track surface, going nowhere."""
    return CarState(
        idx=idx, number=str(idx), name=f"D{idx}", class_id=1, position=idx + 1,
        class_position=idx + 1, lap=0, lap_completed=-1, lap_dist_pct=0.9999,
        progress=-0.0001, speed=0.0, on_pit_road=False, surface=TrackSurface.ON_TRACK,
        on_track=True, gap_ahead=None, gap_behind=None, to_leader=None,
        track_gap_ahead=None, closing_rate=None, car_ahead_idx=None,
    )


def snap(t, state, kind=SessionKind.UNKNOWN, gridded=0):
    """The switcher's view of one frame: the session, plus `gridded` cars on track."""
    session = SessionSnapshot(
        session_time=t, flags=0, state=state,
        is_green=state == SessionState.RACING, is_yellow=False, is_red=False,
        is_checkered=state == SessionState.CHECKERED,
        time_remaining=None, laps_remaining=None, is_last_lap=False,
        event_type="Race", session_kind=kind,
    )
    cars = {i: _car(i) for i in range(gridded)}
    return WorldSnapshot(tick=int(t * 10), session_time=t, session=session,
                         cars=cars, order=list(cars), events=[])


def run(sd, frames):
    """Feed (time, state[, kind[, gridded]]) tuples; return the scene changes it
    asked for."""
    out = []
    for t, state, *rest in frames:
        got = sd.update(snap(t, state, *rest))
        if got:
            out.append((t, got))
    return out


def test_pre_session_then_green_then_finish():
    sd = SceneDirector(SceneConfig(settle=4.0, post_race_hold=30.0))
    frames = [(float(t), SessionState.GET_IN_CAR) for t in range(0, 20, 2)]
    frames += [(float(t), SessionState.RACING) for t in range(20, 40, 2)]
    frames += [(float(t), SessionState.CHECKERED) for t in range(40, 100, 2)]
    frames += [(float(t), SessionState.COOL_DOWN) for t in range(100, 160, 2)]

    assert run(sd, frames) == [
        (4.0, PRE),     # settled after 4s of GET_IN_CAR
        (24.0, PROGRAM_SCENE),          # settled after 4s of RACING
        (134.0, POST),   # the whole chequer, 30s of cool-down, THEN 4s of settling
    ]


def test_finish_is_not_thrown_away():
    """The card must not land on the leader crossing the line, nor on the field
    still crossing it: a race finishes one car at a time, and the sim stays in
    CHECKERED until the last of them is home."""
    sd = SceneDirector(SceneConfig(settle=1.0, post_race_hold=45.0))
    run(sd, [(float(t), SessionState.RACING) for t in range(0, 20, 2)])
    assert sd.current == PROGRAM_SCENE

    # two minutes of chequer: still the programme, however long the field takes
    run(sd, [(20.0 + t, SessionState.CHECKERED) for t in range(0, 120, 2)])
    assert sd.current == PROGRAM_SCENE

    # 44s into the cool-down: still the programme (the winner's lap)
    run(sd, [(140.0 + t, SessionState.COOL_DOWN) for t in range(0, 44, 2)])
    assert sd.current == PROGRAM_SCENE

    run(sd, [(140.0 + t, SessionState.COOL_DOWN) for t in range(44, 60, 2)])
    assert sd.current == POST


def test_round_1_would_have_kept_the_programme_until_the_field_was_home():
    """Watkins Glen, 2026-09-20, session time: chequer at 3167.7, cool-down at
    3270.4 (103s later, the last car home), stream cut at ~3350. The card went up
    at 3232, with cars still racing to the line."""
    sd = SceneDirector(SceneConfig(settle=4.0, post_race_hold=30.0))
    run(sd, [(3100.0 + t, SessionState.RACING, SessionKind.RACE) for t in range(0, 66, 2)])
    frames = [(3167.7 + t, SessionState.CHECKERED, SessionKind.RACE) for t in range(0, 103, 2)]
    frames += [(3270.4 + t, SessionState.COOL_DOWN, SessionKind.RACE) for t in range(0, 80, 2)]
    out = run(sd, frames)
    assert len(out) == 1 and out[0][1] == POST
    assert 3304.0 <= out[0][0] < 3306.0             # 30s into cool-down, plus settle


def test_the_closing_card_survives_the_sim_leaving_the_session():
    """After the race the sim goes to its results screen and the state reads
    INVALID. That is not "Starting Soon"; the closing card stays until a real next
    session (GET_IN_CAR) arrives."""
    sd = SceneDirector(SceneConfig(settle=1.0, post_race_hold=5.0))
    run(sd, [(float(t), SessionState.RACING, SessionKind.RACE) for t in range(0, 10, 2)])
    run(sd, [(10.0 + t, SessionState.CHECKERED, SessionKind.RACE) for t in range(0, 10, 2)])
    run(sd, [(20.0 + t, SessionState.COOL_DOWN, SessionKind.RACE) for t in range(0, 10, 2)])
    assert sd.current == POST
    assert run(sd, [(30.0 + t, SessionState.INVALID, SessionKind.RACE) for t in range(0, 30, 2)]) == []
    assert sd.current == POST
    # a next session really starting is a different matter
    out = run(sd, [(float(t), SessionState.GET_IN_CAR, SessionKind.RACE) for t in range(0, 10, 2)])
    assert out == [(2.0, PRE)]


def test_flicker_does_not_cut():
    """SessionState bounces around session boundaries; chasing it is worse than
    reacting a beat late."""
    sd = SceneDirector(SceneConfig(settle=5.0))
    flicker = [(0.0, SessionState.RACING), (1.0, SessionState.GET_IN_CAR),
               (2.0, SessionState.RACING), (3.0, SessionState.GET_IN_CAR),
               (4.0, SessionState.RACING)]
    assert run(sd, flicker) == []
    assert sd.current is None

    # once it stops bouncing, it settles and cuts
    assert run(sd, [(float(t), SessionState.RACING) for t in (5, 7, 9, 11)]) == [
        (9.0, PROGRAM_SCENE)]


def test_no_session_state_means_hands_off():
    """A feed with no SessionState must never touch the producer's switcher."""
    sd = SceneDirector()
    assert run(sd, [(float(t), None) for t in range(0, 40, 2)]) == []
    assert sd.current is None


def test_practice_does_not_claim_a_race_finished():
    sd = SceneDirector(SceneConfig(settle=1.0, post_race_hold=10.0))
    run(sd, [(float(t), SessionState.RACING, SessionKind.PRACTICE) for t in range(0, 10, 2)])
    run(sd, [(10.0 + t, SessionState.CHECKERED, SessionKind.PRACTICE) for t in range(0, 10, 2)])
    out = run(sd, [(20.0 + t, SessionState.COOL_DOWN, SessionKind.PRACTICE)
                   for t in range(0, 30, 2)])
    # 10s of hold from the cool-down at t=20, then 1s of settling
    assert out == [(32.0, MID)]


def test_time_going_backwards_restarts_the_timers():
    """A scrub back or a session rollover must not leave a hold sitting in a
    future that never arrives (the shape of issue #41)."""
    sd = SceneDirector(SceneConfig(settle=2.0, post_race_hold=30.0))
    run(sd, [(float(t), SessionState.RACING) for t in range(0, 10, 2)])
    run(sd, [(10.0 + t, SessionState.CHECKERED) for t in range(0, 20, 2)])
    assert sd.current == PROGRAM_SCENE

    # session jumps back to the start; the chequer clock must not still be running
    run(sd, [(1.0, SessionState.CHECKERED)])
    assert sd._chequer_at == 1.0
    assert sd.current == PROGRAM_SCENE


# ---------------------------------------------------------------- switcher
class FakeOBS:
    def __init__(self, scenes=(PROGRAM_SCENE, PRE), transitions=("GOW Slam",),
                 fail=False):
        self._scenes, self._transitions, self.fail = scenes, transitions, fail
        self.calls = []

    def get_scene_list(self):
        return type("R", (), {"scenes": [{"sceneName": s} for s in self._scenes]})

    def get_scene_transition_list(self):
        return type("R", (), {"transitions": [{"transitionName": t}
                                              for t in self._transitions]})

    def set_current_scene_transition(self, name):
        self.calls.append(("transition", name))

    def set_current_program_scene(self, name):
        if self.fail:
            raise RuntimeError("websocket closed")
        self.calls.append(("scene", name))


def test_switcher_sets_transition_before_the_cut():
    obs = FakeOBS()
    sw = ObsSwitcher(obs, transition="GOW Slam")
    assert sw.apply(PRE)
    assert obs.calls == [("transition", "GOW Slam"), ("scene", PRE)]


def test_switcher_survives_everything():
    """A broken switcher must never take the director down: a frozen sim camera
    for the rest of the race is far worse than a manual scene change."""
    events = []
    sw = ObsSwitcher(FakeOBS(fail=True), transition="GOW Slam", on_event=events.append)
    assert sw.apply(PRE) is False
    assert any("failed" in e for e in events)

    # missing scene: warned once, then quietly skipped
    events.clear()
    sw = ObsSwitcher(FakeOBS(), on_event=events.append)
    assert sw.apply(POST) is False
    assert sw.apply(POST) is False
    assert sum("no scene named" in e for e in events) == 1

    # missing transition still cuts, using whatever OBS has selected
    events.clear()
    obs = FakeOBS(transitions=())
    sw = ObsSwitcher(obs, transition="GOW Slam", on_event=events.append)
    assert sw.apply(PROGRAM_SCENE)
    assert ("transition", "GOW Slam") not in obs.calls
    assert any("no transition named" in e for e in events)


def test_check_reports_scenes_obs_is_missing():
    sw = ObsSwitcher(FakeOBS(scenes=(PROGRAM_SCENE,)))
    assert sw.check(SceneConfig()) == sorted([MID, POST, PRE])


@pytest.mark.parametrize("state,expected", [
    (SessionState.INVALID, PRE),
    (SessionState.GET_IN_CAR, PRE),
    (SessionState.WARMUP, PROGRAM_SCENE),      # gridded pre-race: show the grid + green live
    (SessionState.PARADE_LAPS, PROGRAM_SCENE),
    (SessionState.RACING, PROGRAM_SCENE),
])
def test_state_maps_to_scene(state, expected):
    sd = SceneDirector()
    assert sd.want(snap(0.0, state)) == expected


@pytest.mark.parametrize("kind,expected", [
    (SessionKind.RACE, PROGRAM_SCENE),      # the race session's GET_IN_CAR IS the grid
    (SessionKind.PRACTICE, PRE),
    (SessionKind.QUALIFY, PRE),
    (SessionKind.UNKNOWN, PRE),             # not knowing is what the holding card is for
])
def test_get_in_car_is_the_grid_only_in_a_race(kind, expected):
    sd = SceneDirector(SceneConfig(grid_cars=3))
    assert sd.want(snap(0.0, SessionState.GET_IN_CAR, kind, gridded=20)) == expected


def test_an_empty_grid_keeps_the_card():
    """Drivers grid themselves with the Grid button, so the race session opens
    onto an empty track. One car alone is not a grid; the card holds until enough
    of the field is on it."""
    sd = SceneDirector(SceneConfig(grid_cars=3))
    for n in (0, 1, 2):
        assert sd.want(snap(0.0, SessionState.GET_IN_CAR, SessionKind.RACE, gridded=n)) == PRE
    assert sd.want(snap(0.0, SessionState.GET_IN_CAR, SessionKind.RACE, gridded=3)) == PROGRAM_SCENE


def test_standing_start_grid_is_on_air_as_it_fills():
    """Round 1, Watkins Glen, 2026-09-20: the race session opened in GET_IN_CAR
    and the field gridded itself over the two-minute countdown; WARMUP was ten
    seconds and there were no parade laps. The card covered the forming grid
    until the operator cut by hand. The programme has to be up once there is a
    grid to look at, not when the countdown runs out."""
    sd = SceneDirector(SceneConfig(settle=4.0, post_race_hold=30.0, grid_cars=3))
    # the tail of qualifying: on the programme through the chequer (cars still on their
    # laps), then a cool-down long enough for the Intermission card
    frames = [(float(t), SessionState.RACING, SessionKind.QUALIFY, 24) for t in range(0, 10, 2)]
    frames += [(float(t), SessionState.CHECKERED, SessionKind.QUALIFY, 24) for t in range(10, 40, 2)]
    frames += [(float(t), SessionState.COOL_DOWN, SessionKind.QUALIFY, 24) for t in range(40, 80, 2)]
    # the race session opens on an empty grid; a car arrives every 4s from t=6
    race = SessionKind.RACE
    frames += [(float(t), SessionState.GET_IN_CAR, race, max(0, (t - 2) // 4)) for t in range(0, 120, 2)]
    frames += [(float(t), SessionState.WARMUP, race, 24) for t in range(120, 130, 2)]
    frames += [(float(t), SessionState.RACING, race, 24) for t in range(130, 150, 2)]
    out = run(sd, frames)
    assert out == [
        (4.0, PROGRAM_SCENE),   # qualifying
        (74.0, MID),            # its closing card, 30s into the cool-down plus settle
        (4.0, PRE),             # the race session opens onto an empty grid: Starting Soon
        (18.0, PROGRAM_SCENE),  # the third car gridded at t=14; settled 4s later, not at 124s
    ]
    assert sd.current == PROGRAM_SCENE


def test_a_flicker_out_of_the_chequer_does_not_restart_the_hold():
    """The cut settles against SessionState flicker; the post-chequer hold did not,
    so one bounced frame put the celebration card another minute away. The ceiling
    on the chequer phase is what this exercises now: no cool-down ever comes."""
    sd = SceneDirector(SceneConfig(settle=4.0, chequer_max_hold=60.0))
    frames = [(float(t), SessionState.RACING) for t in range(0, 20, 2)]
    frames += [(float(t), SessionState.CHECKERED) for t in range(20, 50, 2)]
    frames += [(50.0, SessionState.RACING)]                      # one frame of bounce
    frames += [(float(t), SessionState.CHECKERED) for t in range(52, 130, 2)]
    out = run(sd, frames)
    assert (84.0, POST) in out, out                              # 60s from the FIRST chequer


def test_switcher_reconnects_once_when_obs_went_away():
    """ReqClient does not reconnect, so after an OBS restart every cut failed for the
    rest of the show. Given a factory, a failed cut is retried on a fresh connection."""
    events = []
    fresh = FakeOBS()
    sw = ObsSwitcher(FakeOBS(fail=True), transition="GOW Slam", on_event=events.append,
                     reconnect=lambda: fresh)
    assert sw.apply(PRE) is True
    assert ("scene", PRE) in fresh.calls
    assert any("reconnected" in e for e in events)
