"""Why did no replay fire? Runs the real live director with the decision points narrated.

The mode machine rejects candidates in four different places, and all four look
identical from outside: nothing happens. This wraps ReplayDirector's methods so each
rejection says which gate it hit, then hands off to the normal `pylon live` path so
the thing being observed IS the production code, not a reimplementation of it.

    python tools/replay_why.py ws://simbox:8779 [extra pylon live args...]
"""

from __future__ import annotations

import sys

from pylon.director import replay as R


def instrument() -> None:
    D = R.ReplayDirector
    orig_note, orig_obs = D.note_trouble, D.observe
    orig_safe, orig_start = D.safe_now, D.maybe_start

    def note_trouble(self, snap, car_idx, on_cam):
        c = snap.cars.get(car_idx)
        pos = getattr(c, "position", None)
        before = self.state
        orig_note(self, snap, car_idx, on_cam)
        if before == self.state and before == R.ReplayState.IDLE:
            why = ("car not in snapshot" if c is None
                   else f"position {pos} outside 1..{self.cfg.max_pos}"
                   if (pos is None or pos <= 0 or pos > self.cfg.max_pos)
                   else "already seen this car")
            print(f"[why] TROUBLE #{getattr(c, 'number', '?')} rejected: {why}",
                  flush=True)
        elif self.state == R.ReplayState.ARMED and before != self.state:
            print(f"[why] ARMED from trouble: #{getattr(c, 'number', '?')} P{pos}",
                  flush=True)

    def observe(self, snap, current_shot):
        before = self.state
        orig_obs(self, snap, current_shot)
        if before != self.state and self.state == R.ReplayState.ARMED:
            c = self.candidate
            print(f"[why] ARMED from event: {c.kind} #{c.car_number} P{c.position} "
                  f"score={c.score:.1f}", flush=True)

    def safe_now(self, snap, current_shot):
        ok = orig_safe(self, snap, current_shot)
        if self.state == R.ReplayState.ARMED and not ok:
            t = snap.session_time
            cd = t - self.last_replay_end
            kind = getattr(current_shot, "kind", None)
            if cd < self._cooldown():
                why = f"cooldown {cd:.0f}s < {self._cooldown():.0f}s"
            elif current_shot is None:
                why = "no current shot"
            else:
                why = (f"shot is {kind}, needs LEADER (or a yellow); "
                       f"yellow={snap.session.is_yellow}")
            print(f"[why] armed but NOT SAFE: {why}", flush=True)
        return ok

    def maybe_start(self, snap, current_shot, now=0.0):
        was_armed = self.state == R.ReplayState.ARMED
        age = (snap.session_time - self.candidate.at) if self.candidate else 0.0
        out = orig_start(self, snap, current_shot, now)
        if was_armed and not out and self.state == R.ReplayState.IDLE:
            print(f"[why] DISARMED: age {age:.0f}s (max {self.cfg.max_age:.0f}s), "
                  f"held {snap.session_time - self.armed_at:.0f}s "
                  f"(max {self.cfg.hold_for_lull:.0f}s)", flush=True)
        elif out:
            print(f"[why] *** ROLLING *** {len(out)} moves", flush=True)
        return out

    orig_start_moves = D.start

    def start(self, snap, now=0.0):
        out = orig_start_moves(self, snap, now)
        c = self.candidate
        print(f"[why] start: cand.at={c.at:.1f} cand.session_num={c.session_num!r} "
              f"snap.session.session_num={snap.session.session_num!r} "
              f"return_to={self.return_to:.1f} lead_in={self.cfg.lead_in} "
              f"moves={[(m.kind, m.session_num, round(m.session_time, 1)) for m in out]}",
              flush=True)
        return out

    D.start = start
    orig_tick = D.tick

    def tick(self, frame, now):
        before, left, fs = self.state, self.left, self.failed_seeks
        out = orig_tick(self, frame, now)
        if before != self.state or left != self.left or fs != self.failed_seeks:
            print(f"[why] tick: {before}->{self.state} left {left}->{self.left} "
                  f"st={frame.session_time:.1f} return_to={self.return_to:.1f} "
                  f"(need st < {self.return_to - 1.0:.1f}) "
                  f"failed_seeks={self.failed_seeks}", flush=True)
        if self.failed_seeks != fs:
            print(f"[why] *** SEEK NEVER LANDED *** gave up after settle="
                  f"{self.cfg.settle}s; the excursion is abandoned", flush=True)
        return out

    D.note_trouble, D.observe = note_trouble, observe
    D.safe_now, D.maybe_start = safe_now, maybe_start
    D.tick = tick


if __name__ == "__main__":
    instrument()
    from pylon.cli import main
    sys.argv = ["pylon", "live", *sys.argv[1:], "--replays"]
    raise SystemExit(main())
