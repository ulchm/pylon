# Pylon: Design

Status: **built and running end to end** (see §9). Goal: automate an iRacing
broadcast (camera direction, OBS scene control and data overlays) entirely from
the telemetry data stream, with **no visual parsing**.

This document is both the design and the record of what real data taught us. Where
a section still reads as a plan, it is a plan; §9 says what is actually shipped,
and §14 holds the findings that corrected the original assumptions.

Pylon grew out of a broadcaster built for one league, which also spoke the race
aloud through a cloned voice and pulled championship standings from that league's
own API. Both are gone from this product on purpose: a voice is a taste decision
nobody can make for someone else, and a league API is one deployment's fact. What
is left is the part that generalises, which is the direction.

---

## 1. Why this is tractable

iRacing hands us the two things that would otherwise be the hard parts:

- **A rich real-time data stream.** The iRacing SDK exposes a memory-mapped
  telemetry file updated at 60 Hz, plus a session-info YAML blob. Python
  wrapper: `pyirsdk` (`irsdk`). We read it locally, no screen scraping.
- **Programmatic camera control.** The SDK is *not* read-only. It accepts
  broadcast messages that drive the in-sim TV director: point the camera at a
  car, choose a camera group, toggle live/replay, scrub the replay timeline.

So we never parse pixels. We read structured data, decide what's interesting,
and tell iRacing's own broadcast camera where to look. iRacing renders the
beauty shot; we just direct.

### Data available per tick (subset that matters)
- `CarIdxLapDistPct`: each car's position around the lap, 0.0-1.0
- `CarIdxLapNum`, running order / `CarIdxPosition` / `CarIdxClassPosition`
- `CarIdxOnPitRoad`, `CarIdxTrackSurface` (on track / off / in pit / not in world)
- `CarIdxEstTime` / `CarIdxF2Time`: timing references (we mostly compute gaps ourselves)
- Session flags (green/yellow/white/checkered), laps/time remaining, session type
- Session YAML: driver names, car numbers, classes, track info, camera groups

### Camera control (broadcast messages, via pyirsdk)
- `cam_switch_num(car_number, group, camera)`: follow car #N with a camera group
- `cam_switch_pos(position, group, camera)`: follow whoever is running Pth
- `cam_set_state(...)`: live vs. replay, hide UI, auto-shot on/off
- `replay_search(...)`, `replay_set_play_speed(...)`, `replay_set_play_position(...)`:
  for incident replays (see §7)

Verified against the pyirsdk source in §13.

---

## 2. Development strategy (Linux-friendly)

iRacing runs on the Windows sim PC. We develop on Linux. The whole design is
shaped so that Windows is needed for as little as possible.

**Hide the source behind an interface:**

```
TelemetrySource (protocol)
├── LiveSource      # Windows: reads pyirsdk shared memory (or via the bridge)
└── PlaybackSource  # anywhere: replays recorded frames deterministically
```

Everything downstream (world model, director, OBS, overlays) only sees
`TelemetrySource`.

**Canonical source is the live shared memory, not `.ibt`.** `.ibt` disk files
are a *driving-stint* artifact: only written while you're driving, and finalized
when you leave the car. We broadcast by **spectating**, so no `.ibt` is produced
at all, and even mid-stint it isn't a supported live stream. The live
memory-mapped telemetry, by contrast, is populated for **all cars** while
spectating/replaying/driving. So live memory is the one true production source;
`.ibt` is at most a dev bootstrap (see below).

That gives us these dev modes:

1. **Record then replay (primary dev loop).** The one component that touches the
   live SDK, the **bridge/recorder** (§3, §12), runs in a Win32 context (Windows
   box, or Python-under-Wine) and can write live frames to disk in our own format
   instead of (or as well as) streaming them. Record a real race once by
   *spectating* it, copy the recording to Linux, and replay it through
   `PlaybackSource`. `PlaybackSource` reads our own files (plain Python, no SDK,
   no Win32), so **all director/overlay/OBS dev then runs natively on Linux**,
   deterministic, no sim running. The one-time capture is the only step that
   needs a Win32 context.
2. **Synthetic source (primary Linux dev vehicle).** Real multi-car data is NOT
   available offline, so a scripted `SyntheticSource` generates realistic
   multi-car races with the real `CarIdx*` channel shapes (battles, overtakes,
   incidents, pit cycles). It is the main way to build and unit-test the director
   on Linux before any live capture exists, and the only way to get
   *deterministic* scenarios for tests.

   **`.ibt` is not a data source.** Verified empirically (2026-07) that real
   driver `.ibt` files contain **zero `CarIdx*` channels** (only player-car
   physics/inputs plus session YAML), and they only exist post-stint while
   driving, so they can neither provide multi-car data nor serve a spectating
   broadcast. The single use we made of one was a **one-time extraction** of a
   real roster/track (Red Bull Ring, 20 cars) to seed the synthetic source. After
   that, `.ibt` plays no role. Multi-car data comes only from the live shared
   memory (Win32 recorder) or the synthetic source. See §13.
3. **LAN bridge (live integration).** Same bridge in streaming mode: it reads
   the live SDK and accepts camera commands, exposing both over a WebSocket; the
   director runs on Linux and drives the live sim over the LAN. Same split we'd
   use to put the director on a separate box in production.
4. **Wine/Proton for iRacing. RULED OUT: tested 2026-07-25 (§12).** EasyAntiCheat
   ships no Linux module for iRacing, so the sim cannot be launched under Wine or
   Proton at all: not for racing, not for replays. iRacing + bridge stay on a
   Windows box. This was always the fallback; it is now the only option.

A note on what the synthetic source can and cannot prove: it fakes every channel
*continuously*, including `CarIdxF2Time`. Real iRacing does not (§14), and that
mismatch hid the frozen-interval bug for weeks. Scenario tests that hinge on a
channel's real-world shape need hand-built frames, not synthetic ones.

### Two output domains, two dev assets

The director emits two kinds of output, and they test differently:

- **Data-domain decisions:** who to watch, which battle, scoring, timing,
  overlay values. Computed *purely from telemetry*, so a **telemetry recording**
  exercises them completely. This is the hard/interesting part, and it is fully
  developed and tuned **offline on Linux**.
- **Render-domain actions:** the actual camera cut that produces pixels. Only
  exists when iRacing renders the scene with all its angles.

A telemetry recording can't validate the render domain (no angles, no video,
it's just data). But a **live race isn't required either**: iRacing's own
**replay (`.rpy`)** holds the full 3D scene and every camera angle, the SDK
broadcast **camera messages work in replay mode**, and the telemetry mmap stays
live during replay playback. So:

- **One saved `.rpy` is the master render-domain test bed**: repeatable, same
  race every run; run the bridge against it (Windows/Wine) and the director
  actually cycles cameras against rendered output, no live session.
- The **Linux telemetry recording can be generated from that same `.rpy`** by
  replaying it through the bridge in record mode. One capture, both assets.

**Dev split:** decision logic on Linux against telemetry recordings; camera
behavior on Windows/Wine against a saved `.rpy`. Live sessions only needed for
final end-to-end validation.

### Bridge size, honestly

The **Win32-locked** surface is small and stable (read a frame, send a broadcast
message: a thin layer over pyirsdk). The **substantial** parts (WebSocket server,
session management, command protocol, framing/serialization, backpressure/
reconnect, record-to-disk) are **platform-agnostic Python**, buildable and
unit-testable on Linux with the SDK core stubbed. So the bridge is a real
service, but its Windows-*locked* code stays small. Bonus: **remote manual camera
control is the same command channel**, so director vs. human sender is
indistinguishable to the bridge, and "director assist"/override is nearly free.

---

## 3. Architecture

```
iRacing (shared mem @60Hz)                         [Windows]
      │ pyirsdk
      ▼
┌──────────────┐   bridge (WS)    ┌──────────────────────────────────┐
│ LiveSource / │◄────────────────►│  TelemetrySource                  │
│   bridge     │                  │        │                          │  [Linux dev / Windows prod]
└──────────────┘                  │        ▼                          │
                                  │  Ingest → World Model (per-car)   │
                                  │        │                          │
                                  │        ▼                          │
                                  │  DIRECTOR (scoring + state machine)│
                                  │    │              │               │
                                  │    ▼              ▼               │
                                  │  Camera cmds   OBS + Overlays      │
                                  │  (back to sim)  (obs-websocket,    │
                                  │                  browser source)   │
                                  └──────────────────────────────────┘
```

**Components**
- **Ingest:** pull frames, normalize into a clean per-car world model. Decouple
  decision rate (~5-10 Hz) from telemetry rate (60 Hz).
- **World model:** the single source of truth the director reads (see §4).
- **Director:** the brain. Scores candidate shots, runs the shot state machine,
  emits camera + scene decisions (see §5, §6). This is the hard part.
- **Camera actuator:** translates a chosen shot into SDK broadcast messages.
- **OBS controller:** `obs-websocket` v5 (port 4455) via `obsws-python`; scene
  cuts, graphics packages.
- **Overlays:** a local web page (browser source in OBS) fed telemetry over a
  WebSocket; standings tower, gaps, battle callouts. Decouples *look* from *logic*.
- **Settings:** one commented TOML file the operator edits, directly or through
  the control panel. Everything a show differs by lives there (see §3a).

---

## 3a. The settings seam

Everything above is derived from a telemetry frame. A short list of things cannot
be, and they are exactly the things that differ between one person's broadcast and
another's: what the show is called, what colour it wears, whose cars matter, where
OBS is. That list is the settings file, and keeping it a *list* is the design.

**One file, and it is the whole interface.** `config.py` holds a dataclass per
table and writes the file with a comment above every key, regenerated on each
save, so the documentation for a setting cannot drift from the setting. The
control panel's Settings tab is a form over the same fields; there is no second
source of truth and no hidden state.

**Three rules, and they are the whole design.**

1. *A missing or broken config is not an error.* `load()` always returns a
   `Config`. A file that does not parse is reported through `Config.problems` and
   the defaults stand. The reason is the moment this matters: five minutes before
   a race, with a quote missing. The show goes on air looking wrong rather than
   not going on air, and `pylon doctor` is where the reason is waiting.
2. *Defaults are a working show.* Pylon with no settings at all directs a race.
   The only thing it cannot guess is the obs-websocket password, and it reads that
   out of OBS's own config file, so the first run needs nothing typed.
3. *Unknown keys survive a rewrite.* A key from a newer version, or a note
   somebody added by hand, is kept and written back. An older build must not be
   able to silently delete a newer build's settings.

**`SHOW` is built from it at import.** `settings.ShowSettings.from_config` means a
port changed under `[advanced]` changes the port a worker binds *and* the port the
studio probes, because both read the same object. A config that cannot be read
falls back to the defaults rather than raising, because an exception during
`import pylon.settings` is not recoverable and a show on default ports is.

**Two consumers, two routes.** The workers read the file. The pages get
`GET /show.json` from the overlay's own HTTP server, which is a *reduction*
(`Config.page_view`) carrying a name, a round line and a colour, and no password
and no stream key, because those pages are rendered by a browser on a port with
no authentication. The show's identity is a constant for the session, so it does
not ride the per-frame model.

---

## 4. World model

Per decision tick, build an immutable snapshot:

```
Car:  idx, number, name, class, position, class_position,
      lap_dist_pct, lap_num, speed,
      on_pit_road, surface {on_track|off|pit|not_in_world},
      gap_ahead, gap_behind,           # computed, see below
      gap_ahead_delta                  # closing/opening rate (derivative)

Session: flags, session_type {practice|qual|race},
         laps_remaining / time_remaining, is_yellow, is_last_lap

Events (transient, derived this tick):
      incident(cars, severity), overtake(passer, passed, position),
      pit_entry(car), pit_exit(car), off_track(car), spin(car)
```

**Gaps:** derive from `lap_dist_pct` deltas times track length divided by
relative speed (smoothed), rather than trusting a single SDK timing field. Keep a
short rolling history per car so we can compute *closing rate*, since a gap
shrinking fast is far more interesting than a static one.

**Event detection** is just diffing consecutive snapshots: a position swap
between two adjacent cars is an overtake; `surface` going off-track or a big
negative speed spike is an incident; `on_pit_road` edges are pit entry/exit.

**Sector pace (2026-09-21, `world/sectors.py`).** iRacing publishes sector times
for the player only, and on the broadcast box the player is the spectator: every
`Lap*` / `LapDelta*` channel is dead there and the `CarIdx*` set carries lap
distance, the last lap and the best lap, nothing between. What it does publish is
where the sectors start (`SplitTimeInfo`, forwarded by `LiveSource.session_info`
since this landed), so every car's splits are MEASURED: the session clock at each
boundary crossing, interpolated between the frames either side. Checked on
capture2: 27 flying laps line-to-line against `CarIdxLastLapTime`, mean 0.000s, sd
1ms, worst 3ms. `CarState.pace` (a `LapPace`) is the car's current lap as of the
last boundary: elapsed, the delta to its own best lap and to the session's best lap
at the same boundary (cumulative, the timing-screen delta, never the "ideal lap"),
whether the sector just completed was the quickest of the session (purple) or the
car's own (green), and `done` with the result at the line. Only a lap that began at
the line and stayed out of the pit lane has a pace; both references are laps we
measured and are withheld while the sim's own board disagrees with them (a
broadcast that joins mid-session must not put every car "on pace for the best lap"
against a lap that never was). The lap time itself is still the sim's.

---

## 5. The director: interestingness scoring (the meat)

Each decision tick, enumerate **candidate shots** and score them. A candidate is
usually "follow car X" or "cover the battle between X and Y (adjacent in the
running order)."

### Battle score (adjacent pair)
```
score = closeness      # inverse of gap: <1.0s hot, <2.0s warm, else cold
      * position_weight # P1 fight >> P15 fight; leader/class-leader bonus
      * stakes          # podium / points / last-lap multipliers
      + momentum        # positive gap-closing rate -> bonus
      + side_by_side    # both cars same track section through a corner -> big
      - fatigue         # penalty for a shot we've already held a while
```

- **closeness:** the base signal. Smooth thresholds, not a cliff. Measured on the
  **continuous on-track gap** (`CarState.track_gap_ahead`), NOT `CarIdxF2Time`: see
  §14 for why F2Time is a staircase that turned the whole field into a fake 0.00s
  battle and flat-lined the closing rate.
- **position_weight:** e.g. decaying with position; big bonus for the actual lead
  battle and, in multiclass, for each class lead.
- **stakes:** podium places, last-lap, championship-relevant spots get boosts.
- **momentum:** the derivative matters. A car reeling another in is a story even
  before the gap is small.
- **side_by_side:** the money shot; near-zero gap plus same track segment means
  an overtake happening *now*.
- **fatigue:** a recently-shown battle is docked (decaying over ~18s) so variety
  spreads across the field's live fights instead of ping-ponging one pair. Battles and
  the non-race tour fatigue; the leader is the evergreen fallback.

### The baseline shot depends on whether a race is on (#49)

In a race the baseline is **follow the leader**, deliberately weak (`leader_base` 2.5,
`leader_solo_base` 1.5) so real action outscores it.

**Outside a race there is no leader, so the baseline is a TOUR of the field.** `order[0]`
in practice or qualifying is only whoever is quickest or furthest around the road, and
following it made the camera camp: measured over 120 live cuts at Spa, 59 were LEADER
shots and two cars took 39 of them while fourteen were running. So every car that is
actually running (out of the pits, not in the garage) is a `FOLLOW` candidate at the
**same** base score: rank is deliberately unweighted, because treating a lap-time rank
as a leader is the same mistake in a new place, and it is the one the viewer complained
about.

- **The rotation is not a new mechanism.** Equal scores mean no candidate can steal
  another (`cut_margin` sees to that), so `max_shot` retires each shot on the variety
  timer and `_fatigue` docks the cars just shown, which lands the next pick on somebody
  new. Fatigue is load-bearing here rather than a nicety: without it the variety timer
  hands the camera back to the same two cars forever.
- **What CAN steal is a fresh personal best** (`follow_hot_bonus`, plus
  `follow_best_bonus` when it tops the session), latched for `follow_hot_window` the way
  an incident is latched: a lap is a one-frame event. Both together stay under
  `incident_base`, so a wreck still outranks a fast lap. A car's FIRST sighting adopts its
  time in silence: `pylon live` builds a fresh Director per bridge connection, and
  otherwise a mid-session reconnect reads the whole field's existing times as fresh laps
  at once.
- **And the lap BEFORE the time (2026-09-21):** a car on the session's best pace at
  its last sector boundary (`CarState.pace.on_session_pace(pace_margin)`, 0.10s) gets
  `pace_bonus` (5.0) from the first boundary it clears, which clears `cut_margin` over
  the flat tour, and the shot is STICKY (`Director._on_quick_lap`): not rotated off by
  the variety timer with the lap half done, held through the fresh-best latch at the
  line, then the tour resumes. Up on its own best only is `own_pace_bonus` (1.5), a
  tie-break for the rotation and never a steal: early in a session everybody improves
  on every lap. The same fact is available to anything downstream: the first
  boundary a car is genuinely up on the best lap or quickest through the sector is
  the reason to cut, later boundaries are a running update, and the lap drops off
  the pace or crosses the line short; a lap that comes off is the timing topics'
  moment, with the sim's exact time. Never in a race.
- `ShotKind.FOLLOW` was defined and plumbed end to end (actuator, all three angle
  personalities) with **no producer** until this. Nothing on the render side changed.
- Two real shapes exist and only one is in the captures: an offline/AI practice reports
  `CarIdxPosition` 0 for the whole field (so the old `order[0]` wandered rather than
  camping), while the live official session reported real positions (so it camped). Both
  are toured now; the scored one is pinned by a hand-built test, since no capture has it.

**Aliveness gate (the anti-"why are we watching a car doing nothing" rule).** A
battle only earns its full score when a pass is *imminent* (gap < `side_by_side_gap`)
or the gap is *actually shrinking* (`closing_rate >= closing_thresh`). A pair sitting
at a fixed gap lap after lap is a train, not a fight: its score is multiplied by
`stable_damp` (0.4) so a genuinely live fight or the leader outranks it. Candidacy
also requires green flag, same lap, same class, and neither car on pit road: a car
being lapped or trundling down pit lane is not a battle for position.

**A battle is a RACE thing, and the overlay enforces that itself** (#53). Two cars
running close together in practice or qualifying are sharing a circuit, usually one on
a hot lap and one on an out lap, so neither the row highlight nor the two-car pop-in
bar with its gap chip may appear in a lap-time session: the same principle as the
tower's classification (§11, #40). The gate lives in the overlay rather than only in
the director because the director has no session awareness at all yet (#49) and the
overlay must be right about whatever shot it is handed: a pair in a non-race session
falls through to a single pop-in on the camera's own car. Measured on the practice
capture: 2,846 rows carried the false battle highlight over 3,020 frames, while the
pop-in half never fired there, because battle candidacy needs a position and practice
scores nobody, so the pop-in gate is what keeps this true once #49 lands.

### Event score (interrupts, separate lane)
- **incident:** high priority, scaled by number of cars and whether leaders are
  involved. May trigger a cut and (optionally) a replay.

  **The bar for the interrupt lane is high, and it is severity, not score.** An
  interrupt scores 9-12, so anything that should not have the camera must be
  dropped from candidacy entirely rather than ranked lower: demotion is not
  enough (the same lesson `_shown` teaches for spent interrupts). Only two things
  clear the bar:
  - **the car is gone**: stopped, beached, spun and stationary. Inferred over
    time by the director's trouble detector (`trouble_*` in `director/config.py`),
    which has its own shot kind and sticky hold.
  - **more than one car**: two cars leaving the road within 1.5s of each other
    and within 75m on track. That is the world model's `IncidentSeverity.MAJOR`,
    and it is the one collision signal a per-car detector structurally cannot see.

  A single car running wide is a fast driver using the road, not an incident:
  it never becomes a candidate, so it can neither preempt nor linger.

  **Severity is classified once, in the world model** (`IncidentSeverity`, on the
  `Event`), so the camera and anything downstream of it cannot disagree about how
  big a moment is. One INCIDENT per off-track
  episode, at the tier it earns: MAJOR immediately (a wreck must be able to cut
  instantly), MODERATE after 1.2s still off the road and well down on the pace it
  carried in, MINOR on rejoining for anything that escalated to neither: "that
  was only track limits" is a verdict you can only reach once it is over. The tiers
  are what let contact keep a near-flag priority while track limits fall below an
  overtake and are worth showing only when the camera is already there.
- **overtake for position:** cut to it / replay it.
- **caution:** cut to a wide/incident view; on restart, go to the leader.
- **leader pit stop:** medium. Built 2026-09-21, see below.

Events preempt normal scoring but are subject to an **interrupt cooldown** so the
direction doesn't whip around.

### Pit stops (2026-09-21)

Until this the director had no pit shot at all. Every scorer above excludes a car in
the lane (`_in_pits`), which was right for the failure it guarded against, camping on
a parked car, and wrong for the series: a leader's stop was thirty seconds in which
the leader ceased to exist and the camera followed P2. In a series where the stop
decides races it was the one thing never shown (measured on a real race,
2026-09-20). Two faults, fixed together:

- **The world model could not tell a drive-in from a reset.** `PIT_ENTRY` fired
  whenever `CarIdxOnPitRoad` went true, however the car got there, so a finisher
  pressing Escape and a car back from a tow both read as "a pit stop". The
  discriminator lives in `WorldModel._drove_in`: a car that was out of the world last
  frame, or lands `IN_PIT_STALL` on the frame it appears on pit road, or whose lap
  distance stepped by more than `RESET_STEP_LAPS` (0.05) was put there, and that is
  `EventKind.PIT_RESET`. A drive-in enters through the approach road: measured on
  capture2, the surface reads `APPROACHING_PITS` for three seconds before the flag
  flips; the one Escape on the same capture goes `ON_TRACK` at 24.7% of the lap to
  `IN_PIT_STALL` at 3.5% in a single frame. A reset is never a shot and never a stop
  (not counted, not timed), but its exit is still a `PIT_EXIT`, because the way out is
  always driven. A slot filling at session start is neither: it follows nothing.
- **The stop is a shot.** `ShotKind.PIT`, a normal-lane candidate from the entry
  event until the car leaves the lane (`Director._track_pits`, `PitVisit`), scored by
  the place the car came in FROM, weighted like a battle plus `pit_podium_bonus` for
  the top three: P1 9.0, P2 6.75, P3 6.0, P5 3.4, P10 2.95. So a leader's stop takes
  the camera off everything but a pass in progress at the front, a fight for the win
  and the interrupts, and a midfield stop is what the camera goes to when the leader
  is running alone or a battle has gone stale, never over a live fight. STICKY while
  it lasts (the variety timer cannot cut off a car in its box halfway through the
  tyres), retired by the exit or by `pit_max_hold` (45s: a car being rebuilt is a
  parked car). Offered under green and yellow alike, never after the flag, never
  outside a race. The replay machine will not leave a top-six car's stop for a
  replay, as it will not leave a top-six battle.
- **The rejoin is a second cut.** The lane cameras cannot see the road the car merges
  into, so the exit turns the candidate into a `FOLLOW` with the `REJOIN` flavor for
  `pit_rejoin_hold` (8s), at the same score, framed from the chase and the trackside
  sets. Keyed as the plain follow, so the shot's subject is still just that car.
- **Angles:** `Pit Lane` and `Pit Lane 2` alternate between stops (both groups exist
  on every track seen), Chase and TV1 behind them for a track without.
- **What a shot is worth** is the stop itself: `pit_base` while the car is in the
  lane, released once it has been stationary past `pit_max_hold` so a car stuck in
  its box cannot camp the camera for the rest of the race.

---

## 6. The director: shot selection state machine

Scoring picks *what's interesting*; this layer keeps it *watchable*.

```
every decision tick (~0.5-1s):
  candidates = score_all_shots(world)
  best = max(candidates)

  if interrupt_event and not in_cooldown:
      cut_to(event_shot); start_cooldown()      # incidents/overtakes win

  elif held_time < MIN_SHOT_SECONDS:
      hold()                                     # anti-flicker floor

  elif best.score > current.score + CUT_MARGIN:
      cut_to(best)                               # only switch if clearly better

  elif held_time > MAX_SHOT_SECONDS:
      cut_to(best_other)                         # force variety, avoid staring

  else:
      hold()
```

Key knobs (all config):
`MIN_SHOT_SECONDS`, `MAX_SHOT_SECONDS`, `CUT_MARGIN` (hysteresis),
`HOT_GAP`/`WARM_GAP`, `INTERRUPT_COOLDOWN`.

**Hysteresis is the whole game.** `CUT_MARGIN` plus `MIN_SHOT_SECONDS` stop the
director twitching between near-equal battles every tick; `MAX_SHOT_SECONDS`
stops it from getting stuck on one car.

### Session-phase behavior
- **Start:** pack / first-corner cameras; expect chaos, loosen interrupt rules.
- **Mid-race:** battle-following as above; weave in pit cycles.
- **End:** leader priority plus any live battle for position; last-lap
  multipliers dominate; be ready to cut to a final-corner pass.

### Camera group selection
iRacing exposes ~22 groups per track (Spa capture): trackside `TV1/TV2/TV3` and
the two big mixed sets `TV Mixed` (68 cameras at Watkins Glen) and `TV Static`
(21), in-car `Cockpit/Gyro/Nose/Gearbox/Roll Bar`, `Chase/Far Chase/Rear Chase`,
world cams `Chopper/Blimp/Scenic`, and the pit-lane pair. We use them, not just one
TV group.

**No cockpit on air (2026-09-21).** It is the driver's-eye view and it shows the
mirrors, which the operator does not want on the broadcast; at Round 1 it was one
FOLLOW cut in four (143 of ~630 cuts). The `classic` and `cinematic` personalities
keep only the in-car angles with no mirror in frame (Nose, Gearbox);
`onboard_forward` is the one that still features it, by name. Measured at the same
time: eleven of the track's 22 groups were never cut to at all, the two mixed TV
sets among them, so the follow and leader rotations now draw on those, and TROUBLE
has a profile of its own (it used to fall back to plain TV, so the chopper never
flew).

An **AnglePolicy** (`camera/angles.py`) maps `(shot kind, flavor)` to a ranked
list of logical angles; an **AngleRotator** resolves those to real per-track group
numbers via the `CameraMap`, rotates for variety, drops angles a track lacks, and
never repeats the same group on back-to-back cuts. The director tags shots with a
telemetry-derived **flavor** the render side can't see: `SOLO` (leader running
free -> in-car looks best) and `SIDE_BY_SIDE` (overlapping pair -> chase/nose frame
the wheel-to-wheel). Three shipped personalities (`classic` default,
`onboard_forward`, `cinematic`) set how hard to lean on onboards.

The seam holds: the director decides *what* to watch, the actuator/angle-policy
decide *how* to shoot it, all pure and Linux-tested. Later win: a **per-track map**
of `lap_dist_pct` section to best camera group, so we pick a flattering angle for
where on the lap the car is. Tedious per track, high payoff.

---

## 7. Incident replays: DECIDED, use iRacing's replay

**Decision:** replays are driven through **iRacing's own replay system** (scrub
the sim replay timeline, pick a fresh camera, play it back), *not* an OBS
output-buffer instant-replay.

**Why:** the whole value of an auto-replay is showing something that happened
**off the live camera**, a mid-pack incident we weren't watching. An OBS output
buffer only ever contains what we already broadcast, so it structurally can't
show the missed action. Only iRacing's replay can seek to the moment and shoot it
from any angle.

**Consequence:** the director must own a small **live-to-replay mode machine**:
- On a replay-worthy event, mark the sim timestamp, then `replay_search` /
  `replay_set_play_position` to that moment, choose an angle, roll it (optionally
  slow-mo), then return to live and resync to the leaders.
- Budget: only replay when there's a safe gap to do so (e.g. under yellow, or a
  lull), and cap replay length so we don't miss live developments.
- OBS still gets a "replay" scene/graphics package cut around it.

MVP still **skips auto-replays** (build the live director first), but the
architecture reserves the mode machine for this from day one, since it's a core
requirement rather than a later add-on.

### Measured against the real sim, 2026-07-25

The verbs, the channels and the mode machine are built (`pylon live --replays`,
OFF by default). Three findings from driving an actual sim, which change the design
rather than merely confirm it:

**1. `to_end` does NOT mean "back to live". It means the end of the tape.** That is
the live edge only when the sim is *spectating a live session*. Broadcasting a saved
replay, which is the other production path since the user's own official races
cannot host a second client (section 14), it is the FINISH. Measured on a 6.8h endurance replay
being played 1.3h in: a `to_end` there would have jumped the broadcast 4.5 hours
ahead, to the results screen. The pre-seek value of `IsReplayPlaying` is the
discriminator, and the rule is asymmetric: use `to_end` ONLY on positive evidence of
a live edge, otherwise seek back to the mark. Seeking to the mark is recoverable
everywhere; a wrong `to_end` is not recoverable inside a broadcast.

**2. A replay costs nothing on a tape.** The "cap replay length so we don't miss live
developments" budget assumes a live session carrying on without us. A saved tape does
not advance while we are away, so an excursion misses *nothing* and the return is
exact. Caps differ by an order of magnitude between the two cases.

**3. The abort case cannot be handled.** While the sim is in replay its telemetry
describes the replayed moment, so the live race is unobservable *by construction*:
no channel reports it. Short rolls are the only mitigation, not a detector.

### RESOLVED (2026-07-31): the sim honours every replay verb; the refusal was measurement error

Settled on the rig with `tools/replay_verify.py` (5/5: pause, play, slow motion,
`replay_search_session_time`, `replay_set_play_position`) and through the full
production path (`pylon replay-probe`). Two things produced the false verdict
below. The detection heuristic only watched the frame cursor, so a working 2x read
as "no effect". And `ReplayDirector.start()` sent a play-speed message right behind
the seek, which makes the sim discard the seek: from outside, exactly what "ignores
replay commands" looks like. Speed goes first, always; a test pins the order. The
record of the wrong turn stays because the traps in it are real (the session-scoped
mmap over SSH, end-of-tape parking, out-of-range seeks).

#### The original finding, superseded

On the tested rig, **iRacing accepts camera broadcast messages and ignores every
replay one**, from the same process microseconds apart. `cam_switch_pos` visibly
moved the picture between the Chopper and Cockpit groups; `replay_set_play_speed(0)`
left session time advancing at exactly 1x (+6.00s over a 6s pause), and
`replay_search`, `replay_set_play_position` and `replay_search_session_time` were all
equally ignored.

Ruled out, with evidence, so nobody repeats it:

- **Not the agent build.** A pre-#17 agent raises `ValueError` out of the bridge
  handler on an unknown op and drops the socket; the probe's connection survived
  every replay op and kept streaming. The new replay channels confirm it separately.
- **Not pyirsdk.** `replay_set_play_speed` packs `wParam = 3 | speed<<16`,
  `lParam = slow_motion`, matching the SDK's `MAKELONG` macro.
- **Not the transport.** `_broadcast_msg` is the same `SendNotifyMessageW(HWND_BROADCAST,
  ...)` call the working camera commands use.
- **Not window focus.** Retested with iRacing foreground; unchanged.
- **Not one bad verb.** All four behave identically.

Still untested: whether MANUAL replay control (spacebar / scrub bar) works on that
rig while a file is being played back. That is the next thing to check, because it
separates "the replay is in a non-controllable playback state" from "iRacing refuses
SDK replay messages specifically". `pylon replay-probe <bridge-url>` reproduces the
whole thing in about 20 seconds and prints a verdict.

---

## 8. Tech stack

- **Python 3.12+**, `asyncio` event loop.
- `pyirsdk` (`irsdk`): live telemetry + camera broadcast + `.ibt` offline read.
- `obsws-python`: OBS control (obs-websocket v5).
- `websockets`: the telemetry bridge + pushing data to overlays.
- `dataclasses`: world model snapshots. Stdlib on purpose, see below.
- Overlays: plain HTML, CSS and JavaScript in a browser source. No framework, no
  build step, no bundler. They are served off our own HTTP server to one local
  browser, so there is nothing a build would buy and a file you can open and edit
  is worth a great deal to anyone who wants a different look.
- **Config**: one commented TOML file, read with stdlib `tomllib` and written by
  hand (see §3a). Nothing else parses it.

**Four runtime dependencies, all pure Python bar the SDK.** That is a packaging
decision as much as a taste one: the product ships as a PyInstaller build to
people who will not install Python, and every dependency is weight in a download
and a chance for a frozen build to be missing something. Dropping the speech stack
took the install from gigabytes of torch to tens of megabytes.

---

## 9. Phased roadmap

- **Phase 0 (DONE): plumbing and dev harness.** `TelemetrySource` protocol;
  `PlaybackSource` reading recorded frames; `SyntheticSource`; a recorder (Win32)
  and `describe`/`replay` tools. Delivered: generate and replay recordings on
  Linux; `pylon synth|describe|replay|record`.
- **Phase 1 (DONE): world model.** Ingest + derived per-car speed + gaps +
  closing-rate + event detection (overtake/off-track/incident/pit). Delivered:
  `pylon story` prints the current story each tick (leader, top battles,
  events).
- **Phase 2 (DONE): director (dry).** Interestingness scoring + shot state
  machine (hysteresis + incident interrupt lane), decisions to a **log only**,
  no sim control. Validated on the real Spa race: ~9s avg shot, lead-fight
  priority, incident preemption. `pylon direct`.
- **Phase 3 (command path DONE, Linux-validated): camera control.** `camera/`
  package: an **actuator** (`Shot -> CamCommand`; resolves `target_idx -> car
  number` and logical `TV1/TV2/TV3 -> real per-track group number` from
  `CameraInfo.Groups`), a `CamCommand` wire protocol, a `CameraController` seam
  (`apply()` dispatch), and the **LAN bridge** (`BridgeServer` receiver +
  `WsCommandClient`/`DirectSink` senders + the `broadcast()` driver loop).
  `pylon bridge` / `pylon broadcast`. Validated on the real Spa capture: cuts
  to leader then to each lead battle's attacker, TV1 resolved to group 11 from the
  capture; proven both co-located (`DirectSink`) and split over a real WebSocket
  (`WsCommandClient` -> `BridgeServer`). The **only** Win32-locked piece is
  `SdkCameraController` (`camera/sdk.py`), a thin `pyirsdk` wrapper (its
  delegation is unit-tested against a fake `ir`). **Bidirectional bridge DONE +
  Linux-validated:** the bridge now streams telemetry out *and* takes camera
  commands in over one WebSocket (`BridgeServer(source=...)` +
  `BridgeClient`/`drive_live`); the director runs on the Linux end against streamed
  frames and ships cuts back. `pylon bridge --replay <rec>` + `pylon live
  ws://host:8779` reproduce the full sim->Linux->sim loop on the Spa capture, cuts
  identical to a local `direct` run. The live path has since run for real against
  the sim box (Watkins Glen, 57 drivers). `pylon live` reconnects on its own when
  the bridge drops: it is the one worker whose death freezes the camera wherever it
  last pointed, so it must outlive an agent restart. **Remaining:** the
  render-domain taste pass, i.e. watching the cuts land on screen and tuning which
  angle flatters which moment.
- **Phase 4 (DONE): OBS + overlays.** F1-style timing tower
  (`overlays/broadcast-overlay.html`) driven by real telemetry over WebSocket via
  `pylon overlay`, with multiclass colouring, a follow-cam window, and camera
  pop-ins synced to the *real* director shot (over the bridge echo plus a local
  shotlink file, never a simulated second director). The tower's interval column is
  computed from lap-distance every frame, NOT `CarIdxF2Time`: that channel is a
  per-lap staircase, see §14. OBS scene provisioning is `pylon obs-setup`
  (`obs/setup.py`): a 1920x1080/60 Program scene of the sim's game capture with the
  overlay composited over it, plus three holding cards, built idempotently over
  obs-websocket. `show/obssetup.py` is the same job as one callable, which is what
  the control panel's "Set up OBS" button runs.
- **Beyond the original plan (DONE).** Three things the roadmap never had:
  - **The studio** (`show/studio.py`, `pylon studio`): one launcher. Starts the
    bridge, the overlay and the director in that order, each once the previous one
    is serving, keeps them up, and serves the control panel. (An earlier sim-box
    agent GUI and a two-box split with NDI video were both retired 2026-09: one PC
    does it all.)
  - **The control panel** (`show/dock.py`, `overlays/control.html`): OBS registers
    any URL as a Custom Browser Dock, so the operator surface lives inside the OBS
    window as a page with no plugin, no Qt and nothing to rebuild when OBS bumps
    its ABI. Worker health, restart, "Set up OBS", and the settings form.
  - **Trouble detection**: no per-car damage channel exists over the SDK, so the
    director infers a stricken car (stopped, or slow and off-track, while the field
    flies by) and stays with it. See `DirectorConfig.trouble_*`.
- **Phase 5: polish.** Instant replays are DONE (§7: `director/replay.py` marks the
  moment, waits for a lull in the racing, seeks, rolls and returns; verified on the
  real sim). Still open: per-track camera maps, session-phase behaviors, caution
  packages.
- **Packaging (DONE).** A PyInstaller onedir build, an Inno Setup installer and a
  portable zip (`packaging/`). The studio spawns its workers by re-running
  `sys.executable`, which under a frozen build is the application itself, so
  `default_workers` drops the `-m pylon` it uses from a checkout. The overlay pages
  ship as data beside the executable, not inside the archive, because they are
  served over HTTP to OBS's browser and have to exist as files.

---

## 10. Open questions

- Beyond the F1 timing tower (§11), what else in overlays: battle callouts,
  purple-sector flashes? (The session clock and LIVE/REPLAY badge shipped; see
  §14. So has the fastest lap: the holder's outrigger goes purple and a toast
  fires when the time changes hands, plus a per-car marker for a black flag,
  a disqualification, a meatball or a furled warning: issue #15. Sectors are
  measured for every car since 2026-09-21 (§4, `CarState.pace`) and drive the
  camera; a purple-sector flash on the tower is the open half of #26.)
- How much per-track camera mapping is worth it vs. leaning on iRacing's auto
  camera selection?
- Do we want a manual override / "director assist" mode (human can veto/pin a
  shot) or fully hands-off? The bridge makes this nearly free: a human sender and
  the director are indistinguishable on the command channel (§2).
- Multi-class and ovals both work and have had far less real-session testing than
  road racing. Sector logic in particular assumes a road course's three splits.
- Team races with driver swaps are untested: the world model keys a car's identity
  off the entry, and a swap changes who is in it mid-race.
- Should a human be able to veto or pin a shot? The bridge makes this nearly free:
  a human sender and the director are indistinguishable on the command channel
  (§2). Nothing consumes `Director.talking_about` today, and that hook is where an
  outside voice would ask the camera to hold.

**Answered:** the deployment target is one Windows PC running the sim, OBS and
Pylon. All-Linux via Proton is ruled out, not deferred (§12).

---

## 11. Overlay spec: F1-style timing tower (DECIDED)

Left-hand vertical column, one row per driver in running order, styled like the
F1 world-feed tower.

- **Header cap:** track, a LIVE/REPLAY badge, and a **session badge** (PRACTICE /
  QUALIFYING / WARM-UP / RACE) that is always shown, because a practice tower has
  the same
  rows, gaps and clock as a race one, so nothing else on screen tells the viewer
  these cars are not racing. Non-race sessions take the signature colour; race is a
  quiet outline.
  - **The LIVE/REPLAY badge is a decision of the BROADCAST, not a state of the sim.**
    It says "we cut to tape", so each path sets it outright: the live bridge feed
    forces LIVE, recording playback forces REPLAY. It must never be read off
    `IsReplayPlaying`, which is 1 all session long on a spectator client (§13 findings,
    issue #44). When the live-to-replay mode machine lands (#18) the badge becomes a
    function of the director's mode rather than a constant per path.
- **Follow-cam window:** the top **5 rows are pinned** (the leaders never scroll away)
  and the rest of the field runs through a clipped window of **at most 17 rows** under
  them.
  - **Both boxes are sized to the field**, capped at those maxima: the rows are
    absolutely positioned and so contribute no height of their own, which is why the
    boxes have to be told how tall to be, and a fixed height left half a tower of empty
    glass hanging under an 11-car practice field (#45). The caps live in CSS
    (`--pinned`, `--window`, with the 1080p height budget written next to them) and
    `render()` shadows both on `.tower` with what the current field needs; the script
    reads the caps back out of the stylesheet, so each number exists exactly once. The
    edge fades are suppressed when nothing is off screen: their whole job is to imply
    cars past the edge.
  - **It PAGES toward the on-cam car; it does not track it.** Re-centring on every cut
    made the tower the most restless thing in the frame: a cut from P6 to P21 slid
    fifteen rows under the viewer in half a second and the next cut slid them back
    (#59). So the window is persistent state, not a per-frame function of the camera:
    dead still while the on-cam car is inside it, otherwise stepping **at most 10 rows
    per 4s** toward the page holding that car, and settling back onto the head of the
    field when the camera is on a pinned car or on nothing. Window 17 against a step of
    10 lands the car comfortably inside the window instead of on its edge, and a move
    must be asked for continuously (~0.75s) before it commits, so a car swapping across
    the window's edge cannot pump it. Cadence is measured off `performance.now()` deltas
    inside `render()`, never a timer: a timer pages the tower against stale rows while
    the stream is stalled or reconnecting.
  - **The scroll is one transform on the container.** Rows sit at their absolute rank
    inside `.scroll__inner` and that element translates; paging by rewriting every row's
    own transform instead would fight the reorder animation (seventeen rows sliding
    420px while the incoming ones pop in from nowhere). Each row therefore keeps its
    transform for position changes, and the outrigger keeps riding its row.
  - The on-cam highlight can be **off screen** for a beat after a deep cut. That is the
    trade a stable tower costs, and it needs no compensating behaviour: the pop-in card
    already names the driver the camera is on.
- **Row:** position, car number, driver (abbrev), a **gap column**, and an
  **outrigger** (below). Nothing else. A class badge, a licence chip and an iRating
  were tried in the row and taken back out: the class is already said by the colour
  bar down the row's left edge, and a licence and an iRating are paddock detail
  rather than race state. Both still ride the payload and both still render on the
  pop-in, where the viewer has time to read them. Body plus outrigger is 320px, a
  sixth of a 1920 frame.
- **Outrigger** (IMSA-style, issue #11): a quieter secondary column hung off the
  right of the tower body, carrying **PIT** while a car is on pit road and its
  **last completed lap** otherwise. It exists so the gap column can always show an
  actual gap: PIT used to *replace* the gap, and a pit cycle is precisely when
  positions change and the viewer most wants the number.
  - It is a **grid track of the row itself**, never a parallel list positioned
    alongside the tower. Rows reorder by animating `transform`, so anything outside
    `.row` desyncs from its own row for the whole half-second of every position
    change. As a column it inherits the transform and cannot drift (measured: 57k
    samples across 51 live reorders, zero drift).
  - A car with no completed lap renders **blank**, never a placeholder and never
    iRacing's `-1.0` (see §14).
  - **In QUALIFYING the column is the car's BEST lap, and says "Best"** (#54). There the
    best lap IS the result, while a last lap is as likely to be an in lap, an out lap or
    an aborted run, so the column spent the session showing a number that meant nothing
    while the number deciding it was nowhere on screen. **Practice deliberately keeps the
    LAST lap**, on instruction and for a reason: there the last lap says who is on a hot
    lap right now and whether they are improving. The trap for the next implementer is
    that the neighbouring predicate (`by_lap`, which drives the "Gap" header and the
    lap-time classification) covers practice AND qualifying together, and `bestLap` is
    populated for both, so reaching for it would silently change practice too. The
    outrigger tests the session kind itself. PIT still outranks the time in qualifying: it
    is a live state and it still matters there.
  - **GARAGE** (#46) replaces a stale LAST lap and never a best one: a car in the garage
    is not on a lap, but the best lap is the time it is classified on and taking it off
    the board would remove the session's own result.
- **Track name** comes from `TrackDisplayShortName` ("Spa"), not
  `TrackDisplayName` ("Circuit de Spa-Francorchamps"). The long form does not fit
  the header at any readable size and was being ellipsised mid-word; the short form
  is what the sport says out loud anyway.
- **Gap column cycles between two modes** (like the F1 feed toggling
  leader/interval):
  - **Interval:** gap to the car *directly ahead* (P3 shows gap to P2).
  - **Leader:** gap to P1 (P3 shows gap to the leader).
  - Leader's own row shows the lap/flag or "Leader"; cars a lap down show `+1L`.
- **Cycle:** auto-toggle on a timer (say every ~10-15s), with hooks to force a
  mode (e.g. force Interval when the director is on a close battle).
- **Multiclass:** group/segment by class, per-class leader reference.
- Data comes from the same world model (§4), gaps already computed there, so the
  overlay is a thin view with no separate timing logic.

**Implementation:** a browser source in OBS; a local page subscribes to the
director's WebSocket and re-renders the tower each push. Look/animation in
CSS/JS, decoupled from director logic.

---

## 12. Linux viability and the SDK boundary

**The SDK is two channels, and BOTH are Win32-bound in practice:**
- **Telemetry** is a Windows **named memory-mapped file** (`Local\IRSDKMemMapFileName`),
  created by iRacing at 60 Hz. It's a Windows/Wine kernel object; reading it
  reliably means running the reader *in* a Win32 context. (Native-Linux reads of
  a Wine mapping are fragile and version-dependent, so treat that as a curiosity,
  not a plan.) This is the **canonical live source**; `.ibt` disk files are
  driving-only and finalized on exit (§2), so they can't serve a spectating
  broadcast.
- **Camera / replay control** is **Win32 window messaging**
  (`RegisterWindowMessageW("IRSDK_BROADCASTMSG")` then `SendNotifyMessageW` to
  `0xFFFF`/`HWND_BROADCAST`, verified in §13). **A native Linux process cannot
  send these.**

So reading telemetry *and* sending commands both need Win32; a native-Linux
process could do neither directly.

**Consequence: one bridge owns both directions, quarantining all Windows-ness.**
A single **bridge/recorder** runs in a Win32 context (Windows box, or
Python-under-Wine) and: reads the live mmap, sends the broadcast messages, and
exposes both over a WebSocket (**stream mode**), or writes frames to disk in our
own format (**record mode**). It's a real service, but its **Win32-locked core is
small and stable**; the substantial transport/protocol layer around it is
platform-agnostic and Linux-testable (§2, "Bridge size, honestly"). Everything
else (director, OBS, overlays, and all dev against `PlaybackSource`, which reads
our own files with no SDK) is Linux-native. Under Wine the bridge must share
iRacing's wineprefix/desktop so `HWND_BROADCAST` reaches the sim.

**Deployment options (swappable, decide late):**
- **All-Linux: RULED OUT (tested 2026-07-25).** Was: iRacing under Proton plus the
  bridge in the *same Wine prefix*, one machine, no Windows. **This does not work.**
  Tested end-to-end on CachyOS (RTX 5080, NVIDIA 610.43.03): iRacing installs and
  the sim starts, but launching `start_protected_game.exe` (the EasyAntiCheat
  entry point, and the only supported way in) fails with *"No anti-cheat module
  has been found for this game and platform."* iRacing has migrated to Epic's EOS
  EAC and has not enabled the Linux profile, so no Linux anti-cheat module ships
  with the game. The gate is at **launch**, before any online/offline distinction,
  so **replays and Test Drive are blocked too, not just online racing**. (Note:
  AreWeAntiCheatYet's "Test Drive & AI Racing only" entry predates the EOS
  migration, so do not plan around it.) Only iRacing can fix this, by ticking the
  Linux box in the EOS portal. Working around EAC is circumvention and a ban risk;
  not an option. Revisit only if iRacing announces Linux/Proton support.
- **Windows box + LAN bridge (was CHOSEN; retired 2026-09):** iRacing and the
  bridge on the sim PC; director, OBS and overlays on Linux over the LAN, with
  video crossing separately as NDI. It worked, and it was more than the show
  needed.
- **One Windows PC (CHOSEN, running):** everything on the machine running the sim,
  supervised by `pylon studio`; OBS game-captures the sim locally. The bridge is
  unchanged, it simply serves loopback. Linux stays the dev box: everything but
  `camera/sdk.py` runs and is tested there against recordings.
- **Windows VM with GPU passthrough (fallback):** only if you refuse a bare-metal
  Windows box and Proton won't cooperate; heavier to set up.

**Bottom line:** Linux viability does **not** depend on iRacing running on Linux.
Build to the `TelemetrySource` + bridge interface (§2, §3) and Proton-vs-Windows
becomes a deployment flag, not an architecture decision.

**Verified (§13):** pyirsdk's `IBT` reader uses only `mmap`/`struct`, with no
`windll` and no module-level Win32 calls, so the **entire `PlaybackSource` dev
loop runs natively on Linux with pyirsdk itself, no Wine at all.** Wine/Windows
is needed *only* for live sessions (the bridge). Confirmed on the dev box:
`from irsdk import IBT` imports cleanly under Linux/Python 3.13.

---

## 13. Verified API surface (checked against pyirsdk source, 2026-07)

All signatures/constants below are quoted from `kutu/pyirsdk` `irsdk.py`, the SDK
telemetry `vars.txt`, and `obsws-python`. Cross-checked against firsthand
sources.

### Camera and replay methods (IRSDK class, live only)
```python
cam_switch_pos(position=0, group=1, camera=0)
cam_switch_num(car_number='1', group=1, camera=0)      # car_number is a STRING
cam_set_state(camera_state=CameraState.cam_tool_active)
replay_set_play_speed(speed=0, slow_motion=False)
replay_set_play_position(pos_mode=RpyPosMode.begin, frame_num=0)
replay_search(search_mode=RpySrchMode.to_start)
replay_search_session_time(session_num=0, session_time_ms=0)
replay_set_state(state_mode=RpyStateMode.erase_tape)
reload_all_textures(); reload_texture(car_idx=0)
video_capture(video_capture_mode=VideoCaptureMode.trigger_screen_shot)
```

### Enums / constants
- **RpySrchMode:** to_start=0, to_end=1, prev_session=2, next_session=3,
  prev_lap=4, next_lap=5, prev_frame=6, next_frame=7, **prev_incident=8**,
  **next_incident=9**
- **RpyPosMode:** begin=0, current=1, end=2
- **RpyStateMode:** erase_tape=0
- **csMode (special cam targets, use as `position`):** at_incident=-3,
  at_leader=-2, at_exciting=-1
- **CameraState (flags for cam_set_state):** is_session_screen=0x0001,
  is_scenic_active=0x0002, cam_tool_active=0x0004, **ui_hidden=0x0008**,
  **use_auto_shot_selection=0x0010**, use_temporary_edits=0x0020,
  use_key_acceleration=0x0040, use_key10x_acceleration=0x0080,
  use_mouse_aim_mode=0x0100
- **BroadcastMsg** (for reference; wrappers exist for all): cam_switch_pos=0,
  cam_switch_num=1, cam_set_state=2, replay_set_play_speed=3,
  replay_set_play_position=4, replay_search=5, replay_set_state=6,
  reload_textures=7, chat_command=8, pit_command=9, telem_command=10,
  ffb_command=11, replay_search_session_time=12, video_capture=13

### Broadcast mechanism (why the bridge is mandatory)
```python
BROADCASTMSGNAME = 'IRSDK_BROADCASTMSG'
msg_id = ctypes.windll.user32.RegisterWindowMessageW(BROADCASTMSGNAME)
ctypes.windll.user32.SendNotifyMessageW(0xFFFF, msg_id,          # 0xFFFF = HWND_BROADCAST
    broadcast_type | var1 << 16, var2 | var3 << 16)
```
Pure Win32 windowing IPC, **not sendable from native Linux.** Under Wine, the
bridge must share iRacing's wineprefix/desktop so HWND_BROADCAST reaches it.

### Car number encoding
`car_number` is a **string**; `_pad_car_num` encodes leading zeros so `'1'`,
`'01'`, `'001'` are distinct. Always pass the number as a string.

### `.ibt` files: NOT used (empirically ruled out, 2026-07)
Verified against two real driver `.ibt` files (`teamjorge/ibt` test fixtures and
`gmartsenkov/itelem`'s AI race):
- **Zero `CarIdx*` channels.** `.ibt` logs only player-car channels
  (physics/inputs, `Player*` fields) plus the session-info YAML. It does **not**
  contain the multi-car positional arrays the director needs (confirmed: 0 of 267
  channels start with `CarIdx`).
- **Post-stint only.** Written while *driving on track* and finalized on exit, so
  it never exists for a *spectating* broadcast.
- **Net:** `.ibt` is not a data source. The one thing we took from it was a
  one-time roster/track extraction (session YAML is intact and rich) to seed the
  synthetic source. Multi-car data comes exclusively from the live shared memory
  (Win32 recorder) or `SyntheticSource`.

The pyirsdk `IBT` reader is pure `mmap`/`struct` (no `windll`, imports on Linux);
relevant only to that one-time roster extraction.

### Key telemetry channels (from vars.txt)
- Position/gaps: `CarIdxLapDistPct`, `CarIdxPosition`, `CarIdxClassPosition`,
  `CarIdxLap`, `CarIdxLapCompleted`, `CarIdxEstTime`,
  `CarIdxF2Time` (time behind **leader**, so compute intervals ourselves),
  `CarIdxClass`
- State: `CarIdxOnPitRoad`, `CarIdxTrackSurface`, `CarIdxTrackSurfaceMaterial`,
  `CarIdxGear`, `CarIdxRPM`, `CarIdxLastLapTime`, `CarIdxBestLapTime`
- Flags/pace: `CarIdxSessionFlags` (per-car black/meatball/etc.),
  `CarIdxPaceFlags`, `CarIdxPaceLine`, `CarIdxPaceRow`
- Session: `SessionFlags`, `SessionState`, `SessionTime`, `SessionTimeRemain`,
  `SessionLapsRemain`

### OBS control
`obsws-python` (obs-websocket v5), default port **4455**, Python 3.10+, native on
Linux. `ReqClient(host, port, password)` then e.g.
`set_current_program_scene(name)`.

---

## 14. Real-data notes (Spa capture, 2026-07)

Validated the world model against two live captures at Spa-Francorchamps (28
cars, 15 Hz): `capture` (practice, 250s) and `capture2` (race, 355s). Findings
that corrected assumptions:

- **No reliable green bit.** Normal green-flag racing carries only background
  bits (`servicible|startHidden`); the `green` bit (0x4) only flashes at
  start/restart. Derive green from `SessionState == Racing` and the absence of
  caution/red/checkered, not from the green bit. Caution shows via
  yellow/yellowWaving/caution/cautionWaving bits.
- **Pace car is a real in-world car** (`CarIdx 64`, number "0",
  `CarIsPaceCar=1`). Excluded from the competitor order and gap math.
- **Practice has no positions** (`CarIdxPosition == 0` for all); order falls back
  to track progress. Races populate positions normally. **Anything that SPEAKS OR
  SHOWS a position must handle the zero:** every consumer read it as a real place
  until each learned to treat a field with no running order as having no places at
  all. The timing tower was the same bug wearing a graphic: it answered the
  zero by numbering its own rows, so it printed P1..P28 off "who is furthest around
  the road" while the pop-in over the same car showed a blank. It now shows a dash.
  A race carries the zero too, and not only in practice: on `capture2` **238 frames,
  t=10.8 to t=30.4 (the grid and the run to the green) score nobody**, and the
  invented number churned (car #2 walked P1 to P18 in four seconds) because it was
  ranking a standing grid by lap distance.
  **The dash fixed what the tower CLAIMED; it did not fix what the tower was sorted
  by.** Rows stayed in `snap.order`, which with nobody scored is track progress, so a
  practice tower still reordered itself constantly off who happened to be furthest
  around the lap: measured on the practice capture, **the running order changed on 127
  frames of 3020**. A non-race session is now classified by **lap time** instead
  (`snapshot_to_model`, gated on `is_race_kind` so nothing downstream can disagree
  about whether there is a race on), which is what every real practice and
  qualifying timing screen does, with no-time-yet cars at the bottom keyed on `idx`.
  After: **0 frames of reordering** that were not the field itself changing. The gap
  column becomes the deficit to the quickest car, nothing wears the leader treatment,
  and the cap is sent no lap count: in practice every car is on its own lap and the
  top row's is nobody else's. The dash stays: the row order already conveys the
  classification, so numbering it would add nothing except a number the pop-in would
  have to be taught to agree with (issue #40).
- **Practice is `SessionState.RACING`, so "is it green" cannot tell you "is it a
  race".** Measured on `capture`: state 4 (RACING) for 1423 frames, then 5
  (CHECKERED) for 1527, then 6 (COOL_DOWN) for 70. `is_green` is therefore True for
  the whole of practice, and every race-shaped behaviour fired normally there: a
  winner of a practice session was declared, awarded to whoever was furthest around
  the road. The test is `SessionKind`, never the flag state (`world.is_race_kind`,
  one definition every consumer shares). Practice also ends under a checkered flag,
  which is the end of a session and not a win.
- **An official race weekend's practice is THREE MINUTES, and qualifying is not
  timed at all.** The session table is identical in both captures:
  `Sessions[0]` Practice, `SessionTime "180.0000 sec"`, `SessionLaps "unlimited"`;
  `Sessions[1]` Lone Qualify, `SessionTime "86400.0000 sec"`, **`SessionLaps 2`**;
  `Sessions[2]` Race, `SessionTime "1800.0000 sec"`. Consequences:
  - A Spa lap is ~130s, so a 180s practice is **one flying lap** for a driver who
    leaves the pit lane the instant it goes green, and none at all for most of the
    field. It is a shakedown before qualifying, not a practice programme. The
    no-lap-times path is therefore the NORMAL case for an official session, not an
    early-session edge.
  - **86400 seconds is one day: iRacing's placeholder for a session that is not
    timed**, and it sails straight through the week-long "unlimited" guard on
    `SessionTimeTotal`. Untreated, the overlay draws a 24-hour clock over a
    two-lap qualifying session. The
    discriminator is `SessionLaps` being a NUMBER rather than the string "unlimited",
    so a genuine 24-hour race (which reports the same 86400) keeps its clock.
    Read from the session YAML, which we hold for both captures; the live channels
    during a qualifying session are unverified, since we have no qualifying capture.
  - A session's place in the weekend is not visible from inside it, so
    `SessionSnapshot.next_session_kind` carries it (sessions are numbered in weekend
    order, so the next one is `SessionNum + 1`).
- **`SessionTimeRemain` re-bases onto the COOL-DOWN clock the moment the state
  leaves RACING.** Measured on `capture`, a 180-second practice session: remaining
  winds 118 -> 18 under state 4, then reads **603** under state 5 (CHECKERED) and 5.8
  under state 6 (COOL_DOWN). Past the flag it is not this session's clock at all. The
  world model drops it to None there, because every consumer got it wrong: the
  session reads as over and then as having nine minutes left, and the overlay's
  elapsed (`time_total - time_remaining`) goes NEGATIVE. `SessionTimeTotal` is
  unaffected and still reports the session's length.
- **`WeekendInfo.EventType` is the EVENT, not the session, and it says "Race" all
  weekend.** Both captures report `EventType: 'Race'`: including `capture`, which
  *is* the practice session. The session actually on track is
  `SessionInfo.Sessions[SessionNum]`, indexed by the frame's own `SessionNum`
  channel: three entries in both captures, `SessionType` "Practice" / "Lone Qualify"
  / "Race" (`capture` runs SessionNum 0, `capture2` SessionNum 2). Normalised into
  `SessionKind` (`world/model.py`) because the raw strings are a zoo: qualifying is
  "Lone Qualify" *or* "Open Qualify", testing is "Offline Testing". Prefer the
  frame's `SessionNum` over `SessionInfo.CurrentSessionNum`: the latter rides the
  YAML, which the live overlay re-reads only every 60s.
  **The live path had to be widened for this and needs the sim-box agent
  redeployed.** `LiveSource.session_info` forwarded only WeekendInfo / DriverInfo /
  CameraInfo, so a live broadcast carried no Sessions block at all: verified
  against the running bridge, which answered with exactly those three keys. It now
  sends SessionInfo whole: including the `ResultsPositions` tables that are most of
  its size, because those are the only place a finished session's per-car
  `FastestTime` survives (see the lap-time note below). Captures were never affected:
  `tools/capture_core.py` stores the whole session string.
- **Per-car lap times are absent for most of a session, and a finished session's
  times live somewhere else entirely.** On the practice capture, `CarIdxBestLapTime`
  AND `CarIdxLastLapTime` are the `-1.0` sentinel in **all 3020 frames for all 64
  slots** (zero valid readings), because that capture is the opening ~200s of
  practice and nobody has completed a lap yet (`CarIdxF2Time` had 1296 real readings
  over the same span). The race capture has them: 16,411 real `CarIdxBestLapTime`
  readings against 291,677 sentinel. Meanwhile `capture2`'s session-info header
  carries `SessionInfo.Sessions[0..1].ResultsPositions` (a full 28-car
  classification of the *already finished* practice and qualifying, each with
  `FastestTime` (best 129.7936 practice, 129.083 qualifying), while the running
  race's own `ResultsPositions` is empty. So: **the live channel is the source
  during a session, the results table is the source after it**, and anything that
  wants "who is on provisional pole" reads the latter.
- **`CarIdxSessionFlags` works, and `servicible` is not a flag.** The per-car flag
  array really does carry driver-directed flags, and a six-minute race capture caught
  two real ones. Every nonzero value in `capture2`, decoded:

  | Value | Bits | Car-frames |
  | --- | --- | --- |
  | `0x00040000` | `servicible` | 119,994 |
  | `0x00140000` | `servicible\|repair` (**meatball**) | 115 |
  | `0x000c0000` | `servicible\|furled` (a furled black, i.e. a warning) | 3 |

  `servicible` (`0x040000`) is the trap. The SDK's own comment is "car is allowed
  service (**not a flag**)", and as the counts show it is set on essentially every car
  for essentially the whole race, so **`CarIdxSessionFlags != 0` means "this car
  exists", not "this car has been flagged"**, and an indicator built on the raw value
  marks the entire field from lights to flag. Same shape as the no-reliable-green-bit
  note above: the interesting bits arrive alongside background ones. Mask to the
  driver-flag group (`telemetry.constants.DRIVER_FLAG_MASK`), which the world builder
  does once so `CarState.driver_flags` is nonzero only when something real is being
  shown. The whole group (`black`, `disqualify`, `servicible`, `furled`, `repair`,
  `dq_scoring_invalid`) was missing from the project's constants until #15; the values
  are from `irsdk.Flags`, not from memory. `blue` (being lapped) is deliberately not in
  the mask: it is not a penalty, and in a race with any spread it would light up most
  of the field most of the time. It never appears per-car in either capture anyway.
  Neither capture contains a `black` or a `disqualify`, so those two paths are
  hand-tested only.
- **There is no event or series NAME anywhere in the session-info YAML.** Searched
  all four session strings we hold (both Spa captures plus the two `.ibt` headers in
  `samples/`, a Test and an AI race at the Red Bull Ring): the only matches for
  series/season/event/league are `SeriesID`, `SeasonID`, `LeagueID` and `EventType`.
  There is no `SeriesName`-shaped KEY at all, and the schema is fixed (the ids are
  present-and-zero offline rather than omitted), so this is the schema, not a
  side-effect of these being unofficial sessions. The nearest strings are
  `Category` ("Formula", "Road") and `SessionName` ("PRACTICE"/"QUALIFY"/"RACE").
  A title like "6 Hours of the Nurburgring" therefore has to be resolved from
  `SeriesID` through the iRacing /data API (issue #13).
  **Confirmed on a real OFFICIAL session** (2026-07-25, live bridge: Gesamtstrecke
  VLN, `Official: 1`, `SeriesID: 275`, `SeasonID: 6236`, `SubSessionID: 86947570`).
  An official session populates the ids and nothing else: still no name string
  anywhere. So this is not an artefact of our offline captures.
  Until the /data API lands the overlay shows no event name at all rather than an
  invented one.
- **`CarIdxPosition` is the TIMING SHEET, and it only turns over at the line.** A car
  can complete a pass into turn one and still be listed second for the rest of the lap
 : most of a minute at Spa or Road America. Everything derived from it inherits that
  lag, and on `capture2` three of four passes were still un-flipped when the move
  finished. Consequences, all of them things this project got wrong first:
  - the battle shot targeted the "attacker", so at the pass the camera sat on the car
    driving away up an empty road instead of the fight;
  - the shot key encoded the running order, so the eventual flip read as a *new* shot:
    it dropped the fight's fatigue history and cut to an unrelated battle at the exact
    moment this one paid off;
  - the pop-ins named the winner "attacking" the car it had already passed.
  - **Corrected (2026-07):** the rule here used to be "on-screen reads track position,
    the timing tower keeps `CarIdxPosition`, and the two disagreeing for part of a lap
    is correct, not a bug to reconcile." That was wrong twice over. The premise
    understated the lag badly: "a few corners" is really **85.2 seconds** on the
    worst `capture2` pass (#20 cleared #27 at t=211, the sheet flipped at t=301.7,
    which is a full Spa lap), and **59.8% of every tick a battle graphic was on
    screen** the pop-in printed a place the tower had given to a *different car*: the
    name bug read "#128 · P16" over one car while the tower's P16 row was #4. And
    "not a bug to reconcile" was a licence for two graphics on the same screen to be
    numbered from two different sources, which is indistinguishable from a broken
    overlay. Measured: 59 of 68 position changes landed within 2% of the S/F line.
  **Rule: ONE running order, and it is live.** `builder.live_order` is the sheet with
  completed on-track passes applied, and `CarState.position` is counted off it: the
  tower, the pop-ins and the director all read that one number. After the
  change: 0 disagreements across all four recordings, no duplicated place in 4279
  frames, and passes land in the order when they happen (284 position changes, only
  10.6% at the line, against 87% before).
  Three details, each of which was a bug first:
  - **the two margins are asymmetric.** A car takes a place at `LIVE_PASS_TAKE`
    (~2 m) and gives it back only at `LIVE_PASS_YIELD` (~10.4 m) the other way, with
    the inverted pairs carried frame to frame so the band sits around the CURRENT
    order rather than the sheet's. One symmetric wide margin flickered (23 of 394
    changes reversed inside a second: a 20 m swing in half a second, which two cars
    cannot do); one symmetric narrow margin flickers on noise.
  - **the take margin has to be narrow, because "has cleared" is not transitive.**
    Adjacent swaps with a wide margin trap a car behind one it has clearly passed:
    measured, #128 shown P16 while running 11.2 m clear of the P14 car, stuck behind a
    car it led by 8.8 m. Now 0 such frames.
  - **the loop runs to a fixed point, not a fixed count.** A first lap starts from the
    GRID order, so the pack can rearrange completely before a single position changes;
    capping it at 6 passes left 1014 of 4279 frames half-sorted, and which cars were
    left behind moved frame to frame. Cost is a non-issue: the world model runs at
    ~2000 fps against a 60 Hz feed.
  **The sheet still seeds it, and that is not timidity.** The obvious simplification
  (drop the sheet and sort the field by `progress`) is wrong, and measurably so: on the
  formation lap `CarIdxLapCompleted` is **-1** for everybody, so `progress` is just
  `lap_dist_pct` and a car at 99.9% of the lap reads as nearly a lap ahead of one that
  has just crossed the line. Sorting capture2 by raw progress put car #128 P1 while the
  sheet had it 17th, and disagreed with the sheet by 15+ places in 1400 of 4279 frames.
  Progress is a total order; it is just not a total order over *race distance* until
  every car has completed a lap.
  Two things are deliberately NOT reconciled into it:
  - **a place of 0 still means "this session scores nobody"** and is never filled in
    with a row count. The tower used to do exactly that and showed a churning P1..P28
    by who was furthest around the road, while the name bug over the same car showed a
    blank. Both now show a dash.
  - **gaps to leader stay on `CarIdxF2Time`.** It is the real interval; re-deriving it
    from the live order would trade a lagging number for a noisy one.
- **TrackSurface values seen:** NotInWorld(-1), OffTrack(0), InPitStall(1),
  AproachingPits(2), OnTrack(3).
  - **Corrected (2026-07):** the original reading here was "genuine off-tracks are
    rare, so surface -> OffTrack is a clean incident signal without spamming."
    That is true *of this data* (6 off-tracks in 355s on `capture2`, 2 in 250s on
    `capture`), and it is **a measurement of AI drivers, not of racing**.
    `capture2` is an AI race (`WeekendInfo.Official: 0`, `CarIsAI: 1` for 27 of 28
    drivers), and AI cars do not exploit track limits the way quick humans do: they
    do not use the kerb and the exit road every lap. The assumption was load-bearing
   : it is why any surface transition became a full INCIDENT, and why the camera
    abandoned live racing for a fast lap through the runoff. **An off-track
    transition is a raw signal, not an incident.** Severity now decides (see §5).
    General lesson: an AI field cannot validate anything about *driving standards*.
- **LapDistPct** is a clean 0..1 for in-world cars, and it is the *only* live,
  every-frame gap signal.
- **`CarIdxTrackSurface == NOT_IN_WORLD` is one value doing two jobs**, and telling them
  apart matters. Mostly it means **this slot has nobody in it**: the channel is sized to
  iRacing's 64 slots, so on `capture2`'s 28-car field 71 indices read it for the entire
  session. It is *also* a real entry sitting in its **garage** between runs. In a race
  the distinction does not arise (out of the world means retired or being towed, and a
  retired car has to leave the running order), but in practice and qualifying going in is
  the rhythm of the session, and dropping those cars took drivers and their lap times off
  the board mid-session (observed at Spa, 2026-07-26; issue #46). The discriminator is **a
  lap time we have already watched the car set**, remembered in the world model
  (`WorldModel._last_seen`), never this frame's channels: an empty slot never has one,
  and a time that has been set does not un-set because a channel went quiet. Such a car is
  carried in `snap.cars` with `in_garage=True` and deliberately **not** in `snap.order`:
  the order is who is racing, and everything reading it (shot candidates, gaps, passes,
  the field count) must keep seeing exactly that. Note this is invisible in the captures:
  nobody sets a lap in the practice one at all, so all six recordings digest identically
  before and after the change, and the behaviour is pinned by hand-built frames instead.
- **`CarIdxF2Time` is a per-lap staircase, not a live interval.** It updates only
  when a car crosses the s/f line and reads **0 for every car until they complete a
  lap**. Consequences the director learned the hard way on `capture2`: the whole
  field looks like a 0.00s side-by-side battle for the first ~180s, and mid-lap the
  interval is frozen (the lead pair sat at exactly `0.338s` from t=200-300 while the
  cars actually closed from 23m to side-by-side). So its **time-derivative is
  useless** as a closing rate. Use F2Time only for the *tower's* to-leader/interval
  column (it is the correct race gap, immune to lapped-car folding). For **battle
  detection, proximity, and closing rate**, use the continuous on-track gap off the
  est ruler (`WorldModel._track_gaps` -> `CarState.track_gap_ahead`, the same
  `est_track_gap` rule as the tower's interval; lap-distance over the trailing car's
  speed only for a feed with no `CarIdxEstTime`, since that ruler breathes through
  every slow corner and its derivative read braking as a catch on 7.4% of capture2's
  battle-range frames), trusted only for physically-near, same-lap pairs (progress
  diff < 0.5 lap) so the "+103s lapped-car" fold can't reappear. See §5.
- **Driver credentials (iRating / licence / country) are in `DriverInfo`, and need a
  REAL OFFICIAL session to mean anything.** Verified against an official IMSA session
  (Road America, 60 entries, `WeekendInfo.Official: 1`) because the captures in
  `recordings/` cannot show any of this: an AI field reports `IRating: 0` and
  `LicString: "R 0.00"` for everyone, with `LicColor` the literal **string
  `"0xundefined"`**. What the official session actually contains:
  - `IRating`: a real int (1041-8247 across that grid). Format it (`4.2k`); show
    nothing at all for 0, or an AI entry prints a "0" rating on air.
  - `LicString`: `"A 3.15"`: class letter + safety rating. `LicLevel` is 1-20
    (17-20 = A, 13-16 = B, ...) and `LicSubLevel` is just the SR x100 (`315`).
  - `LicColor`: a real int here, and iRacing's own class colours (`0x0153db` A,
    `0x00c702` B, `0xfeec04` C). **Do not drive the display off it.** Besides the
    offline `"0xundefined"`, it disagreed with `LicString` for 2 of 59 drivers
    (A-class licences carrying the B-class green). Derive the colour from the class
    LETTER, which is what is printed next to it.
  - **`FlairName` is the country** ("United Kingdom", "Poland"), with `FlairID`. This
    is the field to use: no `/data` API lookup needed. `ClubName` is **not** it: it
    is the literal string `"None"` for all 60 entries even in the official session.
  - Sentinels are strings, not nulls: `"None"`, `"-none-"` (the pace car's flair),
    `""`. Normalise them away in `SessionInfo.drivers()`, not at the display end.
  - `FlairName` also carries **non-country values** (`"Global"`, `"Unaffiliated"`) and
    the UK home nations (`"Scotland"`), which are not ISO 3166-1. Both are normal, not
    errors. `flair.py` maps names to flag emoji and returns `""` for these.
  - **One flair is double-encoded in iRacing's own data:** `"Türkiye"` arrives as
    `"TÃ¼rkiye"`. This is NOT our decoding: every other accented string in the same
    blob (driver names like `Philipp Weiß`, `Julian Mühleisen`) is intact. A
    latin-1 -> utf-8 repair round fixes it, but it must run **before** lower-casing:
    `"Ã".lower()` is `"ã"`, which no longer round-trips.
- **Class labels come from the car model + `CarPath`, not a class-name field.**
  `CarClassShortName` is reliably blank, there is no `CarClassName`, and
  `CarClassColor` is `0xffffff` (unset). GT3/GT4/GTE carry the class token in
  `CarScreenNameShort` ("BMW M4 **GT4**"), so those label themselves; GTP/LMDh and
  LMP prototypes ("Cadillac V-Series.R", "Dallara P217") do NOT, so the overlay
  resolver also searches `CarPath` (iRacing's stable internal id, e.g.
  "porsche963**gtp**") and falls back to a prototype hint map (overlay.py
  `_car_class_token` / `_MODEL_CLASS_HINTS`). Extend the hint map as new cars land.
  Two refinements from a **real official multiclass session** (IMSA at Road America,
  60 entries), which the offline AI capture could not have shown:
  - `CarClassShortName` **is** populated in an official session, but it is not a class
    label: the GT3 field reads `IMSA23` and LMP2 reads `Dallara P217`. Only GTP happens
    to read `GTP`. So the per-row *tag* stays resolved from model + path regardless.
  - The token list maps a **match key to a displayed label**, and they are not the same
    thing. The BMW M Hybrid's `CarPath` is literally `bmwlmdh` with no "gtp" anywhere,
    so a scan that returned its own matched key badged the car `LMDH`. **GTP, LMDh and
    Hypercar are one class of car** (LMDh is the chassis regulation, GTP is IMSA's class,
    Hypercar is WEC's), as are **GTD and GT3**; they normalise to `GTP` and `GT3`. Do not
    re-split them into separate display tokens.
  - **Saying a make out loud is a separate problem from badging the class**, and
    anything that speaks needs its own pronunciation whitelist rather than reusing
    the class resolver: a speech engine reads an unknown abbreviation as a word or
    as garbage, so only a make with a known pronunciation can be named. Never widen
    it to "read the model
    string"; `MX-5`, `V-Series.R`, `963` and `P217` are all real values.
  - **Both captures in `recordings/` are a SPEC SERIES**: 28 cars, all `CarScreenNameShort`
    `"SF Lights 324"` / `CarPath` `"superformulalights324"`, `CarClassShortName` blank.
    So they cannot exercise any multi-make behaviour, and anything that varies by make has
    to be tested against a hand-built field. It also means the make is worth
    **nothing** in a one-make field even if it were pronounceable: naming it
    distinguishes no car from any other.
- **`SessionTimeTotal`/`SessionTimeRemain`** give the event clock. Watch the
  **32767-lap** and ~week-long "unlimited" sentinels on
  `SessionLapsRemain`/`SessionTime*`: filter them, don't display them.
- **`IsReplayPlaying` does NOT mean "we are showing tape"**, and it must never drive the
  overlay's LIVE/REPLAY badge on its own (it did, until #44). It answers "is the replay
  viewer active", and a broadcast/spectator client watches the session from inside that
  viewer parked at the live edge, so it reads 1 for an entire live session. Measured at
  Spa, 2026-07-26: the badge sat on REPLAY for a wholly live broadcast and never once
  said LIVE. Whether the broadcast cut to tape is a **director** state; see §11.
- **`ReplayFrameNumEnd` is NOT 0 at the live edge**, and a rule testing it against zero
  re-creates the Spa bug above. It is frames remaining to the end of the tape, which
  makes "0 == live" look airtight; measured on the captures, a client sitting at the
  live edge reports **1, 19, or a flat 1201** depending on how far behind the viewer
  parks. Corrected 2026-07-30 (#62): the earlier note in `telemetry/live.py` claiming
  0 at the edge was wrong, and is what the first attempt at the badge was built on.
- **What separates a live session from a saved replay is whether the tape is still being
  written.** `ReplayFrameNum + ReplayFrameNumEnd` is the tape's total length: fixed on a
  finished file, growing at 60fps while a session records. Measured 2026-07-30:
  Nordschleife `.rpy` over the bridge, every sample summing to exactly **111250**
  (27215+84035, 27299+83951, 27382+83868); both Spa captures climbing steadily
  (1149 -> 11112 -> 13100, and 18413 -> 32354 over ~400s). Applied in
  `world.session.TapeBadge`, which latches on growth (iRacing's replay buffer is
  finite, so a long live session eventually stops gaining frames and must not become a
  replay retrospectively), and biases the first three seconds to LIVE. Against both
  live captures that is **0 REPLAY frames out of 7299**, where the bare flag gave 1441.
- **Weather: most of the channels exist and only three of them are broadcast material.**
  Measured over both real captures (`capture` 4.2 min, `capture2` 5.9 min; the raw
  captures carry all 324 enumerated channels, so this needed no new recording):
  - **`TrackTemp` is the one good dynamic signal.** It moves slowly and in visible
    quantised steps (39.27 -> 40.56 C over six minutes; a single step in four minutes on
    the other capture), which is exactly the shape a "the track has come up two degrees"
    line wants. `TrackTempCrew` was **identical** to it in both captures: do not treat
    it as a second source.
  - **`AirTemp`, `AirDensity`, `AirPressure`, `RelativeHumidity` change every single
    frame and mean nothing at broadcast timescales.** AirTemp's *entire range* over six
    minutes was 0.05 C. Reporting a trend on these is reporting float noise. Use AirTemp
    only as a scene-setting absolute at the start; ignore density, pressure and humidity
    drift entirely. **Ruled out, deliberately.**
  - **`RelativeHumidity` is a 0..1 fraction, not a percentage** (0.39 live, while
    `WeekendOptions.RelativeHumidity` says `'45 %'`). The two sources disagree on both
    units and value.
  - **`WindVel` (m/s) and `WindDir` (radians) are gusty**: every-frame movement over a
    1.9-3.2 m/s range. Only ever say something qualitative, off a smoothed value.
  - **`Skies` and `TrackWetness` are int enums, static per session in this data**
    (`Skies=1` partly cloudy, `TrackWetness=1` dry). Scene-setting, not a story.
  - **The rain path is UNVALIDATED and must be written as such.** `Precipitation`,
    `FogLevel` and `WeatherDeclaredWet` are flat zero in both captures: both sessions
    were dry. Nothing here proves how they behave when it actually rains, so treat any
    rain logic as unverified until a wet capture exists.
  - **`SessionTimeOfDay` is real and updates at 1 Hz** (seconds since midnight), which
    is the honest source for light/evening material.
  - **`WeekendInfo` / `WeekendOptions` carry the same values as strings WITH UNITS**
    (`'25.56 C'`, `'3.22 km/h'`, `'0 %'`, `'N'`), and they disagree slightly with the
    live channels (`WeatherTemp` 25.56 vs `AirTemp` 25.02). Prefer the live channel and
    parse the static strings only for what has no channel.
  - **The live path carries these now:** `LiveSource.DEFAULT_CHANNELS` (what the
    bridge streams) includes the weather channels, so live and recording see the
    same thing. (It was a fixed list without them when this was first written.)
- **Camera groups captured** in `CameraInfo.Groups` (22 at Spa): TV1/TV2/TV3 are
  the broadcast cameras the director will drive in Phase 3; plus Nose, Cockpit,
  Gearbox, Scenic, and per-corner suspension cams.
- **Session-info sections present:** WeekendInfo, SessionInfo (per-session types:
  Practice / Lone Qualify / Race), DriverInfo, SplitTimeInfo, CameraInfo,
  RadioInfo, QualifyResultsInfo.

Captures live in `recordings/` (gitignored). They are now the primary tuning
input for the director (Phase 2), ahead of the synthetic source.
