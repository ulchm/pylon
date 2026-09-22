from collections import Counter
from itertools import pairwise

from pylon.director import DirectorConfig, run
from pylon.director.core import Director, candidates
from pylon.director.model import Shot, ShotKind
from pylon.telemetry import SyntheticSource


def test_director_produces_watchable_shotlist():
    # incident_cars=2: the scripted moment has to be CONTACT to earn an interrupt now,
    # a lone car running wide no longer preempts anything (see test_incident_severity).
    src = SyntheticSource(num_cars=16, duration_s=120.0, hz=10.0, seed=7, incident_at=60.0,
                          incident_cars=2)
    cfg = DirectorConfig()
    decisions = list(run(src, cfg))

    assert len(decisions) >= 3
    assert decisions[0].reason == "opening"

    kinds = {d.shot.kind for d in decisions}
    assert ShotKind.BATTLE in kinds
    assert ShotKind.INCIDENT in kinds  # the scripted off-track should interrupt

    # min-shot is respected between consecutive cuts, except when the later cut is
    # an incident interrupt (which is allowed to preempt).
    for prev, cur in pairwise(decisions):
        if cur.reason != "incident":
            assert cur.time - prev.time >= cfg.min_shot - 1e-6


def test_director_is_deterministic():
    def shots(seed):
        src = SyntheticSource(num_cars=12, duration_s=60.0, hz=10.0, seed=seed)
        return [(round(d.time, 2), d.shot.key) for d in run(src)]

    assert shots(3) == shots(3)


def test_incident_cut_targets_the_incident_car():
    src = SyntheticSource(num_cars=12, duration_s=90.0, hz=10.0, seed=1, incident_at=45.0,
                          incident_cars=2)
    incidents = [d for d in run(src) if d.shot.kind == ShotKind.INCIDENT]
    assert incidents
    # the shot points the camera at the car that had the incident
    assert all(d.shot.target_idx == int(d.shot.key.split(":")[1]) for d in incidents)


#: a session with no running order: tour the field (#49) -------------------------

PRACTICE_WEEKEND = {
    "WeekendInfo": {"TrackDisplayShortName": "Spa", "TrackLength": "6.9293 km",
                    "EventType": "Race"},          # ...and this is why it cannot be used
    "SessionInfo": {"CurrentSessionNum": 0, "Sessions": [
        {"SessionNum": 0, "SessionType": "Practice", "SessionName": "PRACTICE"},
        {"SessionNum": 2, "SessionType": "Race", "SessionName": "RACE"},
    ]},
    "DriverInfo": {"Drivers": [
        {"CarIdx": i, "CarNumber": str(10 + i), "UserName": f"Driver {i}"} for i in range(6)
    ]},
}


def _practice_frames(n_frames=900, *, hz=10.0, bests=None, pits=(), num_cars=6,
                     scored=False):
    """A practice field: everybody circulates, nobody is racing. `bests` is
    {frame: {idx: best_lap}} so a test can hand a driver a new personal best mid-session.

    `scored` chooses which of the two real shapes this is. An offline/AI practice session
    reports CarIdxPosition 0 for the whole field (as the practice capture does), while the
    live official one at Spa reported real positions, which is what made the old leader
    baseline CAMP rather than merely wander: order[0] was a stable car, and it took 23 of
    120 cuts."""
    from pylon.telemetry.frame import Frame

    best = {i: -1.0 for i in range(num_cars)}
    for f in range(1, n_frames + 1):
        t = f / hz
        for idx, bl in (bests or {}).get(f, {}).items():
            best[idx] = bl
        yield Frame(tick=f, session_time=t, values={
            "SessionTime": t, "SessionNum": 0, "SessionState": 4,     # RACING
            # spread around the lap and moving, so nobody is ever "in trouble"
            "CarIdxLapDistPct": [((t * 0.02) + i / num_cars) % 1.0 for i in range(num_cars)],
            "CarIdxPosition": [i + 1 if scored else 0 for i in range(num_cars)],
            "CarIdxClassPosition": [i + 1 if scored else 0 for i in range(num_cars)],
            "CarIdxTrackSurface": [1 if i in pits else 3 for i in range(num_cars)],
            "CarIdxLap": [int(t * 0.02) + 1] * num_cars,
            "CarIdxLapCompleted": [int(t * 0.02)] * num_cars,
            "CarIdxOnPitRoad": [i in pits for i in range(num_cars)],
            "CarIdxBestLapTime": [best[i] for i in range(num_cars)],
            "CarIdxLastLapTime": [best[i] for i in range(num_cars)],
        })


def _practice_snaps(**kw):
    from pylon.telemetry.frame import SessionInfo
    from pylon.world import WorldModel

    wm = WorldModel(SessionInfo(PRACTICE_WEEKEND))
    return [wm.update(fr) for fr in _practice_frames(**kw)]


def test_practice_has_no_leader_to_follow_so_every_runner_is_a_candidate():
    """#49: the camera camped on whichever car topped the order, because the baseline shot
    is "follow the race leader" and practice has no leader. There the baseline is a tour of
    the field instead, at one score for everybody: a lap-time rank must not become a
    reason to watch somebody."""
    cfg = DirectorConfig()
    snap = _practice_snaps(n_frames=60, pits=(2,))[-1]
    cands = candidates(snap, cfg)

    kinds = {s.kind for (s, _sc, _i) in cands}
    assert ShotKind.LEADER not in kinds          # there is no leader in practice
    assert kinds == {ShotKind.FOLLOW}
    follows = [(s, sc) for (s, sc, _i) in cands if s.kind == ShotKind.FOLLOW]
    # every car that is running, and only those: idx 2 is in its pit box
    assert sorted(s.target_idx for s, _ in follows) == [0, 1, 3, 4, 5]
    assert {s.key for s, _ in follows} == {f"follow:{i}" for i in (0, 1, 3, 4, 5)}
    assert {sc for _, sc in follows} == {cfg.follow_base}      # nobody outranks anybody


def test_a_race_still_follows_its_leader():
    """The invariant: the race path is untouched, same shot kind and same weights."""
    cfg = DirectorConfig()
    src = SyntheticSource(num_cars=10, duration_s=20.0, hz=10.0, seed=4)
    from pylon.world import WorldModel

    wm = WorldModel(src.session_info())
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)
    cands = candidates(snap, cfg)

    leaders = [(s, sc) for (s, sc, _i) in cands if s.kind == ShotKind.LEADER]
    assert len(leaders) == 1
    assert leaders[0][0].key == f"leader:{snap.order[0]}"
    assert leaders[0][1] in (cfg.leader_base, cfg.leader_solo_base)
    assert not any(s.kind == ShotKind.FOLLOW for (s, _sc, _i) in cands)


def test_a_fresh_personal_best_earns_the_camera():
    """"If someone is going super fast / personal best or something": the one thing that
    actually happens in a practice session outscores the tour, by enough to clear
    cut_margin, and more again when it tops the whole session."""
    cfg = DirectorConfig()
    d = Director(cfg)
    # everybody has a time; then car 4 improves its own, and later takes the session best
    snaps = _practice_snaps(n_frames=400, bests={
        1: {i: 132.0 + i for i in range(6)},        # adopted in silence: not set in front of us
        100: {4: 134.0},                            # own best (was 136.0), not the session's
        300: {4: 130.0},                            # ...and now the quickest of anyone
    })
    scores = {}
    for i, snap in enumerate(snaps, start=1):
        d.update(snap)
        cands = candidates(snap, cfg, hot=d._hot)
        scores[i] = {s.target_idx: sc for (s, sc, _x) in cands if s.kind == ShotKind.FOLLOW}

    # a time already on the board when we first see the car is NOT a fresh lap
    assert set(scores[2].values()) == {cfg.follow_base}
    # ...an improvement is
    assert scores[101][4] == cfg.follow_base + cfg.follow_hot_bonus
    assert scores[101][4] - cfg.follow_base > cfg.cut_margin      # enough to steal the camera
    assert scores[101][0] == cfg.follow_base                      # and only for that car
    # ...and taking the session best is worth more again
    assert scores[301][4] == cfg.follow_base + cfg.follow_hot_bonus + cfg.follow_best_bonus
    # the elevation expires, so the tour resumes instead of parking on them
    later = int(101 + cfg.follow_hot_window * 10) + 5
    assert scores[later][4] == cfg.follow_base
    # a wreck still outranks a fast lap
    assert max(scores[301].values()) < cfg.incident_base


def test_the_practice_tour_spreads_across_the_field():
    """The measured symptom was concentration: two cars took 39 of 120 live cuts while
    fourteen were running. A tour has to reach everybody and keep the cadence floors."""
    cfg = DirectorConfig()
    d = Director(cfg)
    cuts = [dec for snap in _practice_snaps(n_frames=1200) if (dec := d.update(snap))]

    assert len(cuts) >= 6
    assert all(c.shot.kind == ShotKind.FOLLOW for c in cuts)
    subjects = Counter(c.shot.target_idx for c in cuts)
    assert len(subjects) == 6                       # every car got the camera
    # no car takes more than its fair share plus a rounding cut
    assert max(subjects.values()) <= len(cuts) / 6 + 1
    # consecutive cuts respect the anti-flicker floor, and nobody is shown twice in a row
    for prev, cur in pairwise(cuts):
        assert cur.time - prev.time >= cfg.min_shot - 1e-6
        assert cur.shot.key != prev.shot.key


def test_a_scored_practice_session_is_still_toured_not_camped_on():
    """The live shape, and the one the practice capture cannot reproduce: an official
    practice session DID report positions, so `order[0]` was a stable car and the leader
    baseline camped on it: 23 of 120 cuts on one car while fourteen ran. A classification
    is not a race: it is toured the same way, and P1 gets no more of the camera than P6."""
    cfg = DirectorConfig()
    d = Director(cfg)
    snaps = _practice_snaps(n_frames=1200, scored=True)
    # positions really are populated (which place each car holds is the world model's
    # live order, not the raw channel: see live_places)
    assert all(c.position > 0 for c in snaps[-1].cars.values())
    cuts = [dec for snap in snaps if (dec := d.update(snap))]

    assert all(c.shot.kind == ShotKind.FOLLOW for c in cuts)
    subjects = Counter(c.shot.target_idx for c in cuts)
    assert len(subjects) == 6
    assert max(subjects.values()) <= len(cuts) / 6 + 1
    fewest = subjects.most_common()[-1][1]
    assert subjects[snaps[-1].order[0]] <= fewest + 1           # P1 is nobody special


def test_the_tour_survives_a_field_with_no_lap_times_at_all():
    """The opening of every practice session, and the whole of the practice capture: no
    car has set a time. The rotation still tours rather than falling over or freezing."""
    d = Director(DirectorConfig())
    cuts = [dec for snap in _practice_snaps(n_frames=600) if (dec := d.update(snap))]

    assert len(cuts) >= 3
    assert all(c.shot.kind == ShotKind.FOLLOW for c in cuts)
    assert len({c.shot.target_idx for c in cuts}) >= 3
    assert all("best" not in c.shot.label for c in cuts)


#: an outside claim on the camera --------------------------------------------------
#
# The director cuts on its own cadence, so anything speaking over the pictures can ask
# the fight we were watching could land over a shot of somewhere else and the viewer
# for the shot to be held. The caller says what it is talking
# about and for how long; the camera honours it, within a ceiling.


def _talking(key, seconds):
    """A stand-in for shotlink.talking_left, which reports SECONDS REMAINING and so
    counts itself down. The real one does that against the wall clock; these tests drive
    session time far faster than real time, so this counts down per probe instead. The
    director probes at most once per decision_interval, which is all the test needs:
    the claim runs out on its own, the way a finished line does."""
    left = [float(seconds)]

    def probe(k):
        if k != key:
            return 0.0
        left[0] = max(0.0, left[0] - 0.5)
        return left[0]

    return probe


# A live battle is sticky and holds for minutes, which is correct on air and useless
# for watching the cadence. These force regular rotation so a delayed cut is visible.
ROTATING = {"min_shot": 2.0, "max_shot": 5.0,
            "battle_max_shot": 5.0, "battle_hard_max": 5.0}


def _cuts(cfg, *, talking=None, **src_kw):
    from pylon.world import run as run_world

    d = Director(cfg)
    d.talking_about = talking
    src = SyntheticSource(num_cars=14, duration_s=120.0, hz=10.0, seed=5, **src_kw)
    return [(round(dec.time, 2), dec.shot.key)
            for snap in run_world(src) if (dec := d.update(snap)) is not None]


def test_the_camera_holds_a_shot_a_caller_is_still_talking_about():
    """The whole point: no cut away from the subject of a line that is still playing."""
    cfg = DirectorConfig(**ROTATING)
    free = _cuts(cfg)
    assert len(free) >= 4, free

    # Claim the opening shot and re-run: the cut off it has to wait for the line.
    held_key = free[0][1]
    cuts = _cuts(cfg, talking=_talking(held_key, 6.0))

    assert cuts[0][1] == held_key
    held_for = cuts[1][0] - cuts[0][0]
    assert held_for > free[1][0] - free[0][0], (held_for, free[1][0] - free[0][0])
    # ...and the claim RUNS OUT rather than sticking: the broadcast carries on.
    assert len(cuts) >= 4, cuts


def test_a_wedged_caller_cannot_camp_the_camera():
    """The hold is an outside process telling the director what to do, so it is capped.
    Checked on the clamp directly: through the shot loop it would be a statement about
    how often the director happens to probe, which is not the property that matters."""
    cfg = DirectorConfig()
    d = Director(cfg)
    d.current = Shot(ShotKind.LEADER, "leader:3", 3, "#3")

    d.talking_about = lambda key: 999.0
    assert d._talking_hold() == cfg.vo_hold_max      # not 999
    d.talking_about = lambda key: 1.5
    assert d._talking_hold() == 1.5                  # an ordinary line is honoured whole
    d.talking_about = lambda key: -4.0               # already finished; clock skew
    assert d._talking_hold() == 0.0
    d.talking_about = None
    assert d._talking_hold() == 0.0                  # nobody wired: no hold at all


def test_a_claim_on_another_shot_does_not_hold_this_one():
    """The hold is per SUBJECT. A line about a fight we have already left must not
    freeze the camera on wherever it happens to be now."""
    cfg = DirectorConfig(**ROTATING)
    free = _cuts(cfg)
    cuts = _cuts(cfg, talking=_talking("battle:999:998", 30.0))  # never on camera
    assert cuts[:4] == free[:4]


def test_an_incident_still_takes_the_camera_mid_line():
    """A line about a battle is not worth missing a crash for. The hold sits below the
    interrupt lane, and this is the test that says so."""
    from pylon.world import run as run_world

    d = Director(DirectorConfig())
    d.talking_about = lambda key: 30.0        # every shot claimed, permanently
    src = SyntheticSource(num_cars=16, duration_s=120.0, hz=10.0, seed=7,
                          incident_at=60.0, incident_cars=2)
    reasons = [dec.reason for snap in run_world(src) if (dec := d.update(snap)) is not None]
    assert "incident" in reasons, reasons


def test_a_broken_or_absent_caller_cannot_freeze_the_camera():
    """Failing open is the requirement. With nothing wired (the normal case for
    `pylon direct`, and for a live run with nothing wired in) the cadence is untouched."""
    cfg = DirectorConfig(**ROTATING)
    unwired = _cuts(cfg)                       # nothing wired at all
    wired = _cuts(cfg, talking=lambda key: 0.0)  # wired, but never claiming
    assert wired == unwired


def test_the_leader_shot_now_carries_a_recency_penalty():
    """It was the only shot in the ranking that never got cheaper, so every time
    anything else's penalty bit, the camera went back to the front. The curve is
    shallower and much faster than a battle's, so a quiet race still sits up there."""
    cfg = DirectorConfig()
    d = Director(cfg)
    d._last_end["leader:3"] = 100.0
    d._last_end["battle:1:2"] = 100.0

    assert d._fatigue("leader:3", 100.0) > 0.0                    # it fatigues at all
    assert d._fatigue("leader:3", 100.0) < d._fatigue("battle:1:2", 100.0)   # ...less
    # ...and recovers faster: by 20s the leader is nearly free again
    assert d._fatigue("leader:3", 120.0) < 0.15
    assert d._fatigue("battle:1:2", 120.0) > d._fatigue("leader:3", 120.0) * 2
    # an incident is not on either curve: `_shown` drops it from candidacy instead
    assert d._fatigue("incident:3:100.0", 100.0) == 0.0


def test_a_shot_key_is_exactly_what_the_shared_vocabulary_builds():
    """Every shot key crosses a process boundary and is compared by string equality on
    the far side, so this is the invariant the whole hand-off rests on: the key the
    director gives a shot is what show/contract.subject_of builds from that same shot.

    Both sides build it independently; they used to merely happen to match.
    """
    from pylon.show.contract import subject_of

    src = SyntheticSource(num_cars=14, duration_s=60.0, hz=10.0, seed=5)
    seen = set()
    for dec in run(src):
        shot = dec.shot
        seen.add(shot.kind)
        assert subject_of(shot.kind, shot.target_idx, shot.pair) == shot.key, shot
    assert {ShotKind.LEADER, ShotKind.BATTLE} & seen


def test_a_wedged_caller_is_cut_away_from_within_vo_hold_max():
    """The clamp above is on the VALUE; this is the loop. The hook reports seconds
    remaining and is re-read every tick, so a caller that asked for a long hold and
    then died answers "still holding" on every tick, and clamping that answer changes
    nothing when all the loop asks is whether it is positive. The hold has to be
    bounded on the director's own clock."""
    cfg = DirectorConfig(**ROTATING)
    stuck = _cuts(cfg, talking=lambda key: 999.0)       # every shot, forever
    assert len(stuck) >= 4, stuck
    gaps = [b[0] - a[0] for a, b in pairwise(stuck)]
    assert max(gaps) <= cfg.max_shot + cfg.vo_hold_max + cfg.decision_interval, gaps
