"""Prove the sim honours replay control, with assertions instead of eyeballing.

The exploratory battery (`replay_bench.py --send`) answered the question, but its
"did it land" heuristic only watches the frame cursor, so it labelled a working
`replay_set_play_speed(2)` as "no effect": the speed changed and the tape kept
running, which is exactly what 2x looks like. A months-old conclusion should not be
reversed on a signal that noisy.

So this checks each verb against what that verb specifically promises:

    pause     -> ReplayPlaySpeed == 0 AND the frame cursor stops moving
    play      -> ReplayPlaySpeed == 1 AND the cursor advances at about 60/s
    slow-mo   -> ReplayPlaySlowMotion set AND the cursor advances SLOWER than 60/s
    seek      -> session time lands near the requested mark, not merely "moves"

Run it on the sim box in the CONSOLE session (see run_on_desktop.ps1): the telemetry
mmap is named Local\\IRSDKMemMapFileName, and `Local\\` is per-session, so from an SSH
login the sim is invisible and every check would fail for the wrong reason.
"""

from __future__ import annotations

import time

import replay_bench as rb

TOL_SEEK_S = 3.0        # seek lands within a few seconds; iRacing snaps to keyframes
FPS = 60.0


class Checks:
    def __init__(self):
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, what: str, detail: str) -> None:
        self.rows.append((bool(ok), what, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {what}\n        {detail}", flush=True)

    def report(self) -> int:
        good = sum(1 for ok, _, _ in self.rows if ok)
        print(f"\n=== {good}/{len(self.rows)} checks passed ===")
        for ok, what, _ in self.rows:
            if not ok:
                print(f"  FAILED: {what}")
        return 0 if good == len(self.rows) else 1


def ival(sample: dict, key: str, default: int = -1) -> int:
    """int(sample[key]) treating ONLY None as missing.

    `int(s.get(k) or -1)` is wrong here and cost a failing check: a paused replay
    reports ReplayPlaySpeed == 0, which is falsy, so `0 or -1` yields -1 and a correct
    pause fails its own assertion. Speed 0 is the single most important value in this
    file, so it cannot be the one the idiom mangles.
    """
    v = sample.get(key)
    return default if v is None else int(v)


def send(mid: int, msg: int, v1: int = 0, v2: int = 0, v3: int = 0) -> None:
    wparam, lparam = rb.pack(msg, v1, v2, v3)
    rb.send_notify(mid, wparam, lparam)


def frames_per_sec(ir, secs: float = 2.0) -> tuple[float, dict]:
    """Measured advance rate of the replay cursor, and the final sample."""
    a = rb.snapshot(ir)
    time.sleep(secs)
    b = rb.snapshot(ir)
    df = (b.get("ReplayFrameNum") or 0) - (a.get("ReplayFrameNum") or 0)
    return df / secs, b


def main() -> int:
    ir = rb.connect_sdk()
    mid = rb.msg_id()
    c = Checks()

    base = rb.snapshot(ir)
    session_num = int(base.get("SessionNum") or 0)
    print(f"baseline: {rb.fmt(base)}  SessionNum={session_num}\n")
    if not base.get("IsReplayPlaying"):
        print("NOTE: IsReplayPlaying is false: load a replay or spectate first.")

    # --- get somewhere PLAYABLE first ---------------------------------------
    # ReplayFrameNumEnd is frames REMAINING. Parked at the finish it reads 1, and then
    # the cursor cannot advance no matter what play speed we set, which reads as
    # "pause and play both failed" when in fact both landed. The first run of this
    # script failed exactly that way, because a prev_incident in the earlier battery
    # had thrown the tape to the end.
    remaining = int(base.get("ReplayFrameNumEnd") or 0)
    if remaining < 6000:
        print(f"tape has only {remaining} frames left; backing up to get playing room")
        send(mid, rb.MSG_REPLAY_SET_PLAY_POSITION, 1, (-18000) & 0xFFFF, 0xFFFF)
        time.sleep(2.5)
        base = rb.snapshot(ir)
        print(f"repositioned: {rb.fmt(base)}")
        remaining = int(base.get("ReplayFrameNumEnd") or 0)

    if remaining < 3000:
        print(f"\nSTILL only {remaining} frames of tape ahead. Playback checks cannot "
              f"mean anything from here; seek somewhere earlier and re-run.")
        return 1

    # --- play, so we start from a known state -------------------------------
    send(mid, rb.MSG_REPLAY_SET_PLAY_SPEED, 1, 0, 0)
    time.sleep(1.0)

    # --- pause ---------------------------------------------------------------
    send(mid, rb.MSG_REPLAY_SET_PLAY_SPEED, 0, 0, 0)
    time.sleep(1.0)
    rate, s = frames_per_sec(ir, 2.0)
    c.check(ival(s, "ReplayPlaySpeed") == 0 and abs(rate) < 2.0,
            "pause: replay_set_play_speed(0)",
            f"ReplayPlaySpeed={s.get('ReplayPlaySpeed')} cursor={rate:+.1f} frames/s "
            f"(want speed 0 and a stopped cursor)")

    # --- play ----------------------------------------------------------------
    send(mid, rb.MSG_REPLAY_SET_PLAY_SPEED, 1, 0, 0)
    time.sleep(1.0)
    rate, s = frames_per_sec(ir, 2.0)
    c.check(ival(s, "ReplayPlaySpeed") == 1 and rate > FPS * 0.5,
            "play: replay_set_play_speed(1)",
            f"ReplayPlaySpeed={s.get('ReplayPlaySpeed')} cursor={rate:+.1f} frames/s "
            f"(want speed 1 and roughly {FPS:.0f}/s)")

    # --- slow motion ---------------------------------------------------------
    # The money shot for a replay. slow_motion divides by `speed` rather than
    # multiplying, so speed=2 is HALF rate, not double.
    send(mid, rb.MSG_REPLAY_SET_PLAY_SPEED, 2, 1, 0)
    time.sleep(1.0)
    rate, s = frames_per_sec(ir, 3.0)
    c.check(bool(s.get("ReplayPlaySlowMotion")) and 0 < rate < FPS * 0.9,
            "slow motion: replay_set_play_speed(2, slow_motion=True)",
            f"ReplayPlaySlowMotion={s.get('ReplayPlaySlowMotion')} "
            f"cursor={rate:+.1f} frames/s (want it set, and slower than {FPS:.0f}/s)")

    send(mid, rb.MSG_REPLAY_SET_PLAY_SPEED, 1, 0, 0)
    time.sleep(1.0)

    # --- seek to a session time ---------------------------------------------
    # THE verb the whole feature rests on: mark a moment, jump straight back to it.
    before = rb.snapshot(ir)
    st0 = float(before.get("SessionTime") or 0.0)
    target = max(1.0, st0 - 60.0)
    send(mid, rb.MSG_REPLAY_SEARCH_SESSION_TIME, session_num, int(target * 1000), 0)
    time.sleep(2.5)
    after = rb.snapshot(ir)
    st1 = float(after.get("SessionTime") or 0.0)
    c.check(abs(st1 - target) <= TOL_SEEK_S,
            "seek: replay_search_session_time(mark - 60s)",
            f"asked for t={target:.2f}s, landed at t={st1:.2f}s "
            f"(was {st0:.2f}s, tolerance {TOL_SEEK_S}s)")

    # --- absolute frame positioning -----------------------------------------
    before = rb.snapshot(ir)
    f0 = int(before.get("ReplayFrameNum") or 0)
    send(mid, rb.MSG_REPLAY_SET_PLAY_POSITION, 1, (-600) & 0xFFFF, 0xFFFF)
    time.sleep(2.0)
    f1 = int(rb.snapshot(ir).get("ReplayFrameNum") or 0)
    moved = f1 - f0
    c.check(-900 < moved < -300,
            "frame seek: replay_set_play_position(current, -600)",
            f"cursor moved {moved:+d} frames (want about -600, allowing for playback "
            f"during the settle)")

    send(mid, rb.MSG_REPLAY_SET_PLAY_SPEED, 1, 0, 0)
    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
