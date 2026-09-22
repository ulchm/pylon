"""Tunable knobs for the director. All time values are seconds."""

from __future__ import annotations

from dataclasses import dataclass

from ..world import IncidentSeverity


@dataclass(frozen=True)
class DirectorConfig:
    # --- who this broadcast is about -------------------------------------------
    # Car NUMBERS the operator asked to see more of, as they are written on the car
    # ("64", "17"). Every shot whose subject is one of them scores `favourite_bonus`
    # higher, which is the difference between a friend mid-pack getting screen time
    # and never appearing at all.
    #
    # A bonus, deliberately, and not a lock: a director that simply follows one car is
    # not directing, and the fight for the lead should still win when there is one. The
    # default is a shade over `cut_margin`, so a favourite can take the camera from an
    # ordinary shot but loses to real drama, which scores far higher than this.
    #
    # Numbers, not car indices: an index is a grid slot that means nothing to a person
    # and changes between sessions. Matching is on the string iRacing reports, with
    # whitespace stripped, so "07" and "7" are different cars, exactly as they are on
    # the timing screen.
    favourites: frozenset[str] = frozenset()
    favourite_bonus: float = 4.0

    # cadence / hysteresis (DESIGN.md section 6)
    decision_interval: float = 0.5   # how often to re-evaluate normal shots
    min_shot: float = 6.0            # anti-flicker floor: never cut sooner (except interrupts).
                                     # 4.0 -> 6.0 (operator, 2026-09-14: "switching a bit too often")
    max_shot: float = 12.0           # force variety after this long on one shot
    battle_max_shot: float = 20.0    # rotate off a STALE battle after this; a live one is
                                     # sticky (held as long as it stays live: see below)
    battle_hard_max: float = 240.0   # absolute backstop: even a live fight yields eventually,
                                     # so stuck telemetry can never camp the camera forever.
                                     # High on purpose: a good battle can hold for several laps.
    cut_margin: float = 3.5          # a challenger must beat the current shot by this to steal it
                                     # (2.5 -> 3.5: make a held shot stickier, fewer cuts)
    pass_window: float = 5.0         # seconds after a lead-change to keep the pass angle,
                                     # treat the fight as "live" (worth holding), and bar
                                     # a second re-point (a pair trading places must not
                                     # ping-pong the camera)
    pass_prox_laps: float = 0.03     # laps of on-track separation under which a pair is
                                     # "nose to tail" for lead-change detection
    pass_confirm: float = 0.4        # seconds the new TRACK order must hold before we call
                                     # it a pass. Passes are detected on track position, not
                                     # CarIdxPosition (which flips at the line, most of a lap
                                     # late), and side-by-side cars trade the lead by inches
                                     # through a corner: without this, every wobble is a pass

    # what counts as a battle. Gaps here are the CONTINUOUS on-track gap
    # (CarState.track_gap_ahead, off the est ruler), NOT the F2Time interval: F2Time
    # is a per-lap staircase that reads 0 until a car completes a lap, so it flags
    # the whole field as a 0.00s battle at the start and hides every catch mid-lap.
    battle_max_gap: float = 1.0      # seconds; pairs wider than this aren't a battle at all
    side_by_side_gap: float = 0.3    # seconds (~21m); at/under this it's ALWAYS a live battle:
                                     # nose-to-tail, worth showing regardless of closing rate, and
                                     # it earns the wheel-to-wheel camera angles + the intensity bonus
    battle_hold_gap: float = 0.8     # seconds; a pair this close is a live battle worth HOLDING
                                     # (stays sticky even between closing bursts), so we don't
                                     # bail to the lone leader during a lull in a real fight
    solo_gap: float = 2.0            # leader is "running free" (in-car angle) when the
                                     # car behind is further back than this (or unknown)

    # aliveness: a battle only earns full score when something is actually happening.
    # A pair sitting at a fixed gap lap after lap is a train, not a fight, and staring
    # at the trailing car is the "why are we watching a car doing nothing" failure.
    closing_thresh: float = 0.02     # s/s of on-track closing that counts as "active"
    stable_damp: float = 0.4         # score multiplier for a not-active (static, not
                                     # side-by-side) battle, so live shots outrank it
    fatigue_amp: float = 1.6         # recency penalty applied to a recently-shown shot
    fatigue_tau: float = 18.0        # seconds; the recency penalty decays over this
    # The leader's own recency curve. It used to have none at all (the only shot in
    # the ranking that never got cheaper), so whenever anything else's penalty bit, the
    # camera went back to the front, over and over. Shallower and much faster than the
    # main curve so the front is still the fallback in a quiet race (full value back
    # inside half a minute) without being where the broadcast keeps landing.
    leader_fatigue_amp: float = 1.1
    leader_fatigue_tau: float = 9.0

    # The ceiling on an outside hold: the longest anything may keep the camera on a
    # shot by asking (Director.talking_about, which Pylon leaves unset). A ceiling, not
    # a target, and what stops a caller that wedged from camping the camera for the
    # rest of the race. Interrupts ignore it entirely.
    vo_hold_max: float = 6.0

    # interrupt lane
    interrupt_cooldown: float = 8.0  # min seconds between interrupt cuts
    # The bar for preempting the broadcast. Only two things clear it: the car is GONE
    # (the trouble detector below), or MORE THAN ONE CAR is involved (the world model's
    # MAJOR tier). A single car running wide is a fast driver using the road, and
    # interrupting live racing for it was the whole complaint: an interrupt scores
    # 9-12, so anything that shouldn't have the camera has to be dropped from candidacy
    # entirely rather than merely ranked lower (the lesson `_shown` already taught us).
    # Do not CUT to an incident that was not already on camera. Off by default,
    # because on its own it would mean an off-camera crash is never shown at all:
    # it is only safe when replays are enabled to cover it, so `pylon live
    # --replays` is what turns it on.
    #
    # The reasoning: cutting live to a car that has already stopped shows the
    # aftermath, which is the least interesting frame of the whole incident. The
    # replay shows it properly: run-up at real speed, then slow motion on the hit.
    # An incident on a car we are ALREADY watching is the opposite case: that is
    # live drama as it happens, and it stays.
    interrupt_only_if_on_camera: bool = False

    incident_min_severity: str = IncidentSeverity.MAJOR
    incident_max_pos: int = 15       # ignore incidents further back than this (backmarker offs)
    incident_hold: float = 8.0       # an incident is a ONE-FRAME event (a surface change), so
                                     # it is latched this long to stay a scorable candidate.
                                     # Without the latch its score vanishes the next tick and
                                     # the state machine cuts away the instant min_shot expires,
                                     # pinning every incident to exactly min_shot seconds.

    # "in trouble": a car crashed / beached / spun: something we should stay with. We
    # can't read other cars' damage over the SDK, so we infer it from telemetry: a car
    # essentially STOPPED, or slow and OFF-TRACK (in the gravel/grass after a hit), while
    # the field flies by under green. We cut to it and STAY (sticky) until it drives away
    # or pits. Deliberately narrow: a pit lane brake or a slow hairpin must NOT trip it.
    trouble_stopped_speed: float = 5.0   # m/s (~18 km/h); at/below this a car is parked/stalled
    trouble_speed: float = 12.0          # m/s (~43 km/h); this-slow AND off-track = spun/beached
    trouble_recover_speed: float = 22.0  # m/s; back above this = recovered, release the hold
    trouble_recent: float = 5.0          # only a car that was at racing pace within this many
                                         # seconds counts: a crash decelerates FROM speed, so a
                                         # gridded car at a standing start (never fast yet) is skipped
    trouble_confirm: float = 0.8         # seconds of sustained trouble before we believe it
    trouble_hold: float = 25.0           # sticky hold on a stricken car, then move on
    trouble_base: float = 12.0           # interrupt priority: outranks a plain off-track incident

    # scoring weights (DESIGN.md section 5)
    pos_k: float = 1.8               # position importance: weight = 1 + pos_k / position_ahead.
                                     # Deliberately modest: a great midfield scrap should beat
                                     # a static gap up front, not lose on track position alone.
    momentum_w: float = 16.0         # bonus per (s/s) of closing rate (real catches ~0.02-0.09)
    momentum_cap: float = 2.2        # ceiling on the momentum bonus (guards rate spikes)
    side_by_side_bonus: float = 0.8  # intensity boost when nose-to-tail (added before pos weight)
    pass_bonus: float = 3.5          # boost for a pair that just swapped places: an actual
                                     # overtake is the best racing there is; go and hold there
    # Touring a session that has no running order (#49). Practice and qualifying have no
    # leader: order[0] is only whoever is quickest or furthest around the road, and camping
    # on them was 49% of a live practice broadcast (59 of 120 cuts, two cars taking a third
    # of it between them). So there every RUNNING car is a candidate at the SAME base score
    # (no position weight, because a rank that means nothing must not become a reason to
    # watch somebody), and the rotation comes from fatigue plus the max_shot variety timer
    # rather than from a score that changes.
    follow_base: float = 2.0
    follow_hot_bonus: float = 4.0     # ...unless the driver has just improved their own best.
                                      # Clears cut_margin comfortably, so the camera swings to
                                      # a car that has actually done something.
    follow_best_bonus: float = 2.5    # ...and more again when that time tops the whole session.
                                      # The two together stay under incident_base: a wreck
                                      # still outranks a fast lap.
    follow_hot_window: float = 8.0    # how long a fresh personal best stays elevated. Long
                                      # enough to win a cut and hold min_shot, short enough
                                      # that the tour resumes rather than parking there.
    # ...and the lap BEFORE the time: a car on a quick one. The world measures every
    # car's sectors (world/sectors.py), and a car up on, or within `pace_margin` of, the
    # session's best lap at the last boundary is the one thing in a practice session
    # worth cutting to while it is still happening. The shot is then HELD to the line
    # (sticky, like a live battle) rather than rotated off with the lap half done, and
    # the fresh-best latch above takes over at the line for the result. Sized like that
    # latch: clear of cut_margin over the tour's flat base, under incident_base.
    pace_bonus: float = 5.0
    pace_margin: float = 0.10         # seconds off the best lap's split that still counts
    own_pace_bonus: float = 1.5       # up on their OWN best only: a tie-break when the tour
                                      # rotates, never enough to steal the camera

    leader_base: float = 2.5         # baseline "follow the leader" score when the leader still has
                                     # a chaser within reach (a lead fight in the making)
    leader_solo_base: float = 1.5    # ...but a leader running by itself is the LOWEST-priority
                                     # evergreen fallback: any live battle should outrank it, so the
                                     # camera doesn't sit on a car driving alone while gaps close
    solo_leader_cut_margin: float = 0.2  # a developing battle only has to modestly beat a lone
                                     # leader to steal the camera (vs the full cut_margin between two
                                     # battles), so we swing to real action promptly, but a static
                                     # train (damped well below the solo base) still can't grab it
    podium_bonus: float = 0.4        # extra stakes when the fight is for a podium spot
    last_lap_mult: float = 2.0       # ...and on the last lap, or with the chequer out and the
                                     # pair still to cross: the line settles it, so it is the
                                     # most a fight is ever worth

    # The finish (world.finish.FinishTracker; Round 1, 2026-09-20: the camera left a
    # 0.04s fight for P4 at the flag, sat on the winner for 24s while that pair crossed
    # unseen, then walked down the order as finishers reset to the pits).
    flag_lead: float = 8.0           # seconds from the line at which the winner's crossing
                                     # takes the camera: on the last lap, or once the chequer
                                     # is out with the leader still to cross (a timed race)
    flag_base: float = 11.0          # the winner taking the flag. Above an incident (9), so
                                     # an off in the pack cannot steal the moment; below
                                     # TROUBLE (12) and below a fight for the WIN itself
                                     # (a P1 last-lap dice scores 14+), which is a better
                                     # picture of the same crossing
    finish_base: float = 2.5         # each later finisher coming to the line, weighted by
                                     # place like a battle (1 + pos_k / position), so a lone
                                     # car crossing loses to any live fight for the line and
                                     # P2 crossing beats P12 crossing
    finish_linger: float = 4.0       # how long a car that has just crossed stays the shot:
                                     # the flag, the first wave, then on to the next one
    incident_base: float = 9.0
    incident_leader_bonus: float = 3.0  # incident involving a top-3 car

    # Pit stops (2026-09-21). Until this the director had no pit shot at all: every
    # scorer above excludes a car in the lane, so a stop by the race leader was thirty
    # seconds in which the leader ceased to exist and the camera followed P2, and in
    # a series where the stop decides races, the stop was the one thing never shown.
    # A stop is a normal-lane candidate from the moment the world reports a DRIVE-IN
    # (EventKind.PIT_ENTRY; a reset never is one) until the car leaves the lane, then
    # the same car is followed out of it for a beat. Weighted by the place the car came
    # in from, like a battle (P5 3.4, P10 2.95), plus the podium bonus for the top three
    # (P1 9.0, P2 6.75, P3 6.0): a leader's stop is the strategy of the race being
    # decided, and it takes the camera off everything but a pass in progress at the
    # front, a fight for the win and the interrupts. A midfield stop is what the camera
    # goes to when the leader is running alone or a battle has gone stale, and never
    # over a fight that is actually alive.
    pit_base: float = 2.5
    pit_podium_bonus: float = 2.0    # ...for a car coming in from the top three
    pit_max_hold: float = 45.0       # a stop stays a candidate this long from the entry;
                                     # past it the story has been told (a car being repaired
                                     # for a minute is a parked car)
    pit_rejoin_hold: float = 8.0     # ...and the car is followed this long out of the lane,
                                     # so the viewer sees who it comes out behind. Under
                                     # max_shot, so the follow ends itself
