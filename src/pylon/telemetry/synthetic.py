"""SyntheticSource: scripted multi-car races for offline dev and tests.

Real multi-car data only exists in the live shared memory, so this is how we
build and tune the director on Linux before any live capture exists. It is also
the only way to get *deterministic* scenarios (a specific battle, a specific
incident) to assert against.

It is fully self-contained: it generates its own roster (numbers/names are
cosmetic and irrelevant to the director) and emits the same real `CarIdx*`
channel shapes the live SDK produces. The real roster/track will come from the
live session YAML at capture time; nothing here depends on an external seed.

Scenario baked in (deterministic given `seed`):
  - a field spreading out from a standing grid,
  - a sustained nose-to-tail battle between two mid-field cars (gap oscillates,
    occasionally side-by-side),
  - an off-track incident with a local yellow: one car by default, or (with
    `incident_cars=2`) the two battling cars making contact.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterator

from .constants import MAX_CARS, SessionFlag, SessionState, TrackSurface
from .frame import Frame, SessionInfo

# A pool of plausible race numbers; includes leading-zero forms on purpose so
# downstream car-number-as-string handling gets exercised.
_NUMBER_POOL = [
    "1", "2", "3", "4", "5", "7", "9", "11", "14", "16", "18", "23", "24",
    "27", "33", "44", "51", "63", "77", "88", "99", "00", "01", "08",
]


class SyntheticSource:
    def __init__(
        self,
        *,
        num_cars: int = 20,
        duration_s: float = 180.0,
        hz: float = 10.0,
        seed: int = 1234,
        incident_at: float = 90.0,
        incident_cars: int = 1,
        track_name: str = "Test Circuit",
        track_length_m: float = 4280.0,
        roster_path: str | None = None,
        classes: list[tuple[str, int, int]] | None = None,
    ):
        self.num_cars = num_cars
        self.duration_s = duration_s
        self.hz = hz
        self.dt = 1.0 / hz
        self.seed = seed
        self.incident_at = incident_at
        self.incident_cars = incident_cars
        self.track_name = track_name
        self.track_length_m = track_length_m
        # classes: list of (short_name, 0xRRGGBB color, count), fastest class first.
        # None = one spec-class field, as before.
        self.classes = classes

        rng = random.Random(seed)
        self._drivers = self._build_roster(rng, roster_path)

        # Per-car pace: leaders slightly faster, small random spread.
        base = 90.0
        self._cars: list[dict] = []
        for grid, d in enumerate(self._drivers):
            self._cars.append(
                {
                    "idx": d["CarIdx"],
                    "number": d["CarNumber"],
                    "class_id": d["CarClassID"],
                    "lap_time": base + grid * 0.15 + rng.uniform(-0.3, 0.3),
                    "grid": grid,
                }
            )

        # Force an adjacent mid-field battle: equalise pace of two cars.
        self._battle_pair = None
        if len(self._cars) >= 8:
            a, b = self._cars[6], self._cars[7]
            b["lap_time"] = a["lap_time"]
            self._battle_pair = (a["idx"], b["idx"])

        # This track's CarIdxEstTime span: the full-lap value of the ruler emitted below
        # (#61). Taken from the FIELD, not from a car, because the channel is a property
        # of the track: see frames(). The median rather than the mean so one crawling
        # incident car cannot drag the ruler off the pace everyone else is running.
        laps = sorted(c["lap_time"] for c in self._cars)
        self.est_lap_s = laps[len(laps) // 2] if laps else base

        # Incident car(s). One car by default: a mid-field runner running wide, which
        # is deliberately NOT interrupt-worthy any more (see #24). With incident_cars=2
        # the scripted battle ends in contact instead, that pair is the only one
        # guaranteed to still be nose-to-tail when the incident fires, so it is the
        # only way to script a deterministic MULTI-car moment.
        self._incident_idx = self._cars[min(10, len(self._cars) - 1)]["idx"] if self._cars else None
        self._incident_other: int | None = None
        if incident_cars >= 2 and self._battle_pair:
            self._incident_idx, self._incident_other = self._battle_pair[1], self._battle_pair[0]
        # a lone excursion crawls (it also reads as 'in trouble'); two cars that touch
        # gather themselves up and continue, damaged.
        self._incident_slow = 0.25 if self._incident_other is None else 0.5

    def _build_roster(self, rng: random.Random, roster_path: str | None) -> list[dict]:
        if roster_path:
            with open(roster_path) as f:
                data = json.loads(f.read())
            drivers = data.get("drivers", [])[: self.num_cars]
            return [
                {
                    "CarIdx": d.get("car_idx", i),
                    "CarNumber": str(d.get("number", i + 1)),
                    "UserName": d.get("name") or f"Driver {i + 1}",
                    "CarScreenNameShort": d.get("car") or "GT3",
                    "CarClassID": d.get("class_id") or 0,
                    "CarClassShortName": d.get("class_name") or "GT3",
                }
                for i, d in enumerate(drivers)
            ]
        classes = self._class_by_grid()
        numbers = _NUMBER_POOL.copy()
        rng.shuffle(numbers)
        roster = []
        for i in range(self.num_cars):
            number = numbers[i] if i < len(numbers) else str(100 + i)
            cid, cname, ccolor = classes[i]
            d = {
                "CarIdx": i,
                "CarNumber": number,
                "UserName": f"Driver {i + 1}",
                "CarScreenNameShort": cname,
                "CarClassID": cid,
                "CarClassShortName": cname,
            }
            if ccolor is not None:
                d["CarClassColor"] = ccolor
            roster.append(d)
        return roster

    def _class_by_grid(self) -> list[tuple[int, str, int | None]]:
        """Per-grid (class_id, short_name, color); fastest class fills the front."""
        if not self.classes:
            return [(0, "GT3", None)] * self.num_cars
        seq: list[tuple[int, str, int | None]] = []
        for k, (name, color, count) in enumerate(self.classes):
            seq += [((k + 1) * 10, name, color)] * count
        if not seq:
            return [(0, "GT3", None)] * self.num_cars
        while len(seq) < self.num_cars:
            seq.append(seq[-1])
        return seq[: self.num_cars]

    def session_info(self) -> SessionInfo:
        raw = {
            "WeekendInfo": {
                "TrackDisplayName": self.track_name,
                "TrackLength": f"{self.track_length_m / 1000.0:.2f} km",
                "EventType": "Race",
            },
            "DriverInfo": {
                "Drivers": [
                    {**d, "CarIsAI": True, "CarIsPaceCar": False} for d in self._drivers
                ]
            },
        }
        return SessionInfo(raw)

    def frames(self) -> Iterator[Frame]:
        n = int(self.duration_s * self.hz)
        track = self.track_length_m
        dist = {c["idx"]: -c["grid"] * 8.0 for c in self._cars}
        base_v = {c["idx"]: track / c["lap_time"] for c in self._cars}

        if self._battle_pair:
            a, b = self._battle_pair
            v = min(base_v[a], base_v[b])
            base_v[a] = base_v[b] = v
            dist[b] = dist[a] - 5.0  # trailing car starts 5 m back

        for tick in range(n):
            t = tick * self.dt
            in_yellow = self.incident_at <= t < self.incident_at + 30.0
            in_incident = self.incident_at <= t < self.incident_at + 8.0
            hit = {self._incident_idx, self._incident_other} - {None}

            for c in self._cars:
                idx = c["idx"]
                v = base_v[idx]
                if self._battle_pair and idx == self._battle_pair[1]:
                    v *= 1.0 + 0.03 * math.sin(2 * math.pi * t / 18.0)
                if in_incident and idx in hit:
                    v *= self._incident_slow
                dist[idx] += v * self.dt

            ldp = [-1.0] * MAX_CARS
            lap = [-1] * MAX_CARS
            lapc = [-1] * MAX_CARS
            pos = [0] * MAX_CARS
            cpos = [0] * MAX_CARS
            cls = [-1] * MAX_CARS
            onpit = [False] * MAX_CARS
            surf = [TrackSurface.NOT_IN_WORLD] * MAX_CARS
            f2 = [-1.0] * MAX_CARS
            est = [-1.0] * MAX_CARS

            order = sorted(self._cars, key=lambda c: dist[c["idx"]], reverse=True)
            leader_dist = dist[order[0]["idx"]] if order else 0.0
            class_rank: dict[int, int] = {}
            for p, c in enumerate(order, start=1):
                idx = c["idx"]
                dd = max(dist[idx], 0.0)
                completed = int(dd // track)
                ldp[idx] = (dd % track) / track
                lap[idx] = completed + 1
                lapc[idx] = completed
                pos[idx] = p
                class_rank[c["class_id"]] = class_rank.get(c["class_id"], 0) + 1
                cpos[idx] = class_rank[c["class_id"]]  # per-class position, as the SDK reports
                cls[idx] = c["class_id"]
                f2[idx] = (leader_dist - dist[idx]) / max(base_v[idx], 1.0)
                # CarIdxEstTime: seconds from the start/finish line to this car's track
                # position (#55, #61). ONE ruler for the whole field (est_lap_s, never
                # this car's own lap_time), because that shared-ness is the entire
                # property est_track_gap rests on. A per-car value would still populate
                # the channel and would make every cross-car difference meaningless.
                #
                # Linear in lap distance, where the real channel is deliberately NOT: the
                # real map already knows La Source is slow and Kemmel is fast, and this
                # track has no speed profile to encode. So this exercises the plumbing,
                # the start/finish wrap and the sentinel handling, not the nonlinearity.
                #
                # Floored just above zero rather than allowed to hit it: 0.0 is how the
                # builder spells "no reading", and an in-world car on the line has one.
                # The real channel behaves the same way, resetting to ~0.0001 rather than
                # to 0 (it is already reset while lap_dist_pct still reads 1.00000).
                est[idx] = max(ldp[idx] * self.est_lap_s, 1e-4)
                is_off = in_incident and idx in hit
                surf[idx] = TrackSurface.OFF_TRACK if is_off else TrackSurface.ON_TRACK

            flags = SessionFlag.GREEN | (SessionFlag.YELLOW if in_yellow else 0)
            values = {
                "SessionTime": t,
                "SessionTick": tick,
                "SessionState": SessionState.RACING,
                "SessionFlags": flags,
                "SessionTimeRemain": max(self.duration_s - t, 0.0),
                "SessionLapsRemain": -1,
                "CarIdxLapDistPct": ldp,
                "CarIdxLap": lap,
                "CarIdxLapCompleted": lapc,
                "CarIdxPosition": pos,
                "CarIdxClassPosition": cpos,
                "CarIdxClass": cls,
                "CarIdxOnPitRoad": onpit,
                "CarIdxTrackSurface": surf,
                "CarIdxF2Time": f2,
                "CarIdxEstTime": est,
            }
            yield Frame(tick=tick, session_time=t, values=values)
