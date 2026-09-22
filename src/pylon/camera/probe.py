"""Replay probe: prove the sim actually honours a seek, from the Linux brain.

Every layer below this is unit-tested on Linux against a fake `ir`, and none of that
proves anything about the sim. Delegation being correct does not mean iRacing does what
you expect: the same unverified render-domain gap Phase 3 still carries. This is the
smallest thing that closes it: mark the clock, seek back, watch the telemetry, come home.

It answers, with evidence rather than inference:

  - does `replay_search_session_time` move the tape, and does it land where asked?
  - does `IsReplayPlaying` flip, i.e. is the discriminator the design leans on real?
  - how long does a seek take to show up in telemetry? (the mode machine has to wait
    for it, and the answer decides whether it can wait on the flag at all)
  - how do we get BACK, and how fast? Not one answer: `to_end` means the end of the
    tape, which is the live edge only when the sim is spectating live. Broadcasting a
    saved endurance replay (this project's other production path) that makes it the
    FINISH. See home_commands().

Run it against a live bridge with nobody watching the stream: it moves the actual
broadcast picture. It is deliberately a separate command rather than part of the
director, because the first time you point this at a sim you want to be holding it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .bridge import BridgeClient
from .command import CamCommand, RpySrchMode


@dataclass
class Sample:
    wall: float          # seconds since the probe armed
    session_time: float
    is_replay: bool | None   # None when the feed does not carry IsReplayPlaying
    frame_from_end: int | None


@dataclass
class ProbeResult:
    mark: float = 0.0              # live session time when we seeked
    target: float = 0.0            # where we asked the tape to go
    session_num: int = 0
    carries_flag: bool = False     # does this feed have IsReplayPlaying at all?
    seek_seen_after: float | None = None    # wall seconds until session time moved back
    landed_at: float | None = None          # session time we actually arrived at
    flag_seen_after: float | None = None    # wall seconds until IsReplayPlaying went True
    returned_after: float | None = None     # wall seconds until back at/after the mark
    stream_ended: bool = False              # the bridge hung up before the probe finished
    playing_tape: bool | None = None        # was the sim ALREADY in replay before we seeked?
    returned_by: str = ""                   # which verb we used to come home, and why
    samples: list[Sample] = field(default_factory=list)

    @property
    def landing_error(self) -> float | None:
        return None if self.landed_at is None else self.landed_at - self.target

    def report(self) -> str:
        header = (f"session {self.session_num}, marked at t={self.mark:.1f}s, "
                  f"asked for t={self.target:.1f}s ({self.mark - self.target:.0f}s back)")
        out = ["", "=== replay probe ===", header, ""]
        if self.stream_ended:
            out += ["TELEMETRY STOPPED mid-probe: the bridge hung up (agent restarted, or",
                    "      the source ran out). Findings below are from a partial run.", ""]
        if self.seek_seen_after is None:
            out += [
                "SEEK: NO EFFECT. Session time never went backwards.",
                "",
                "  The sim did not honour replay_search_session_time. Check, in order:",
                "   - is the agent on the sim box the NEW build? (an old one drops the op)",
                "   - is iRacing focused/running where the agent can broadcast to it?",
                "   - does a manual scrub in the sim work at all right now?",
                "  Every replay design in #16-#20 rests on this working, so stop here.",
            ]
        else:
            err = self.landing_error
            out += [
                f"SEEK: worked. Telemetry moved back after {self.seek_seen_after:.2f}s wall.",
                (f"      landed at t={self.landed_at:.1f}s ({err:+.1f}s vs asked)"
                 if err is not None else ""),
            ]
        if not self.carries_flag:
            out += [
                "",
                "FLAG: this feed does NOT carry IsReplayPlaying.",
                "      The agent predates the channel being added. The mode machine does",
                "      not depend on it (we know we seeked), but the overlay's REPLAY",
                "      badge cannot work until the agent is rebuilt.",
            ]
        elif self.flag_seen_after is None:
            out += ["", "FLAG: IsReplayPlaying never went True, even though the tape moved.",
                    "      Do not gate anything on it."]
        else:
            out += ["", f"FLAG: IsReplayPlaying went True after {self.flag_seen_after:.2f}s wall."]
        mode = ("live edge (a to_end returns to live)" if self.playing_tape is False
                else "PLAYING A SAVED TAPE: to_end here means the FINISH, not live"
                if self.playing_tape else "unknown (no IsReplayPlaying in the feed)")
        out += ["", f"MODE BEFORE SEEK: {mode}"]
        if self.returned_by:
            out += [f"      came home by: {self.returned_by}"]
        if self.returned_after is None:
            out += ["", "RETURN: FAILED, still behind the mark when the probe ended.",
                    "      This is the dangerous failure: a broadcast stuck in the past.",
                    "      Do not enable auto-replays until this works."]
        else:
            out += ["", ("RETURN: back where we started after "
                         f"{self.returned_after:.2f}s wall.")]
        return "\n".join(x for x in out if x != "")


def home_commands(res: ProbeResult) -> list[CamCommand]:
    """How to come back from a replay, which is NOT one answer.

    `RpySrchMode.to_end` means "the end of the tape". When the sim is spectating a live
    session that IS the live edge, which is what every replay issue assumes. But this
    project's other production path is broadcasting a saved endurance replay (DESIGN
    section 14: the user's own races can't have a second client, so they are replay
    based), and there `to_end` means the FINISH: measured live on 2026-07-25, a
    to_end from 1.3h into a 6.8h tape would have jumped the broadcast four and a half
    hours ahead, to the results screen.

    `IsReplayPlaying` BEFORE we seek is the discriminator: false means we were at a
    live edge, anything else means we were already somewhere on a tape.

    Seeking back to the mark is safe in every case, so it is the default and the
    unknown-case answer: at worst it leaves a live broadcast a few seconds behind the
    edge, which the next seek or a human can fix. to_end is only used when we have
    positive evidence we came from live, because its failure mode is unrecoverable
    inside a broadcast.
    """
    if res.playing_tape is False:
        res.returned_by = "search(to_end): we started at a live edge"
        return [CamCommand.replay_search(RpySrchMode.TO_END, label="probe: back to live")]
    why = ("the sim was already playing a tape" if res.playing_tape
           else "cannot tell (feed has no IsReplayPlaying)")
    res.returned_by = f"seek back to the mark: {why}, so to_end would mean THE FINISH"
    return [CamCommand.replay_seek(res.session_num, res.mark, label="probe: back to the mark")]


async def _next(frames):
    """The next frame, or None if the stream ended. A bridge can hang up at any moment
    (the sim-box agent restarts, a replay source runs out), and a probe that raises
    there throws away the evidence it has already gathered."""
    try:
        return await anext(frames)
    except StopAsyncIteration:
        return None


async def probe_replay(url: str, *, back: float = 30.0, hold: float = 8.0,
                       slow_motion: bool = False, on_sample=None) -> ProbeResult:
    """Seek back `back` seconds, watch for `hold`, return to live, and report."""
    client = BridgeClient(url)
    await client.connect()
    res = ProbeResult()
    try:
        frames = client.frames()

        # 1) settle: take the live clock from a few real frames, not the first one
        live_t = 0.0
        for _ in range(10):
            fr = await _next(frames)
            if fr is None:
                res.stream_ended = True
                return res
            live_t = fr.session_time
            res.session_num = int(fr.get("SessionNum") or 0)
            flag0 = fr.get("IsReplayPlaying")
            res.carries_flag = flag0 is not None
            res.playing_tape = None if flag0 is None else bool(flag0)

        res.mark = live_t
        res.target = max(0.0, live_t - back)

        # 2) seek
        await client.send(CamCommand.replay_seek(res.session_num, res.target,
                                                 label=f"probe -{back:.0f}s"))
        if slow_motion:
            await client.send(CamCommand.replay_speed(2, slow_motion=True, label="probe slow-mo"))

        t0 = time.monotonic()
        while time.monotonic() - t0 < hold:
            fr = await _next(frames)
            if fr is None:
                res.stream_ended = True
                break
            wall = time.monotonic() - t0
            flag = fr.get("IsReplayPlaying")
            s = Sample(wall, fr.session_time,
                       None if flag is None else bool(flag),
                       fr.get("ReplayFrameNumEnd"))
            res.samples.append(s)
            if on_sample is not None:
                on_sample(s)
            # "the tape moved" = session time went meaningfully behind the mark. Half a
            # second of slop so a jittery frame at the live edge is not a false positive.
            if res.seek_seen_after is None and fr.session_time < res.mark - 0.5:
                res.seek_seen_after = wall
                res.landed_at = fr.session_time
            if res.flag_seen_after is None and s.is_replay:
                res.flag_seen_after = wall

        # 3) home. Speed first so a slow-mo probe does not crawl its way back.
        await client.send(CamCommand.replay_speed(1, label="probe: real time"))
        for cmd in home_commands(res):
            await client.send(cmd)

        t1 = time.monotonic()
        while time.monotonic() - t1 < max(10.0, hold):
            fr = await _next(frames)
            if fr is None:
                res.stream_ended = True
                break
            if fr.session_time >= res.mark:
                res.returned_after = time.monotonic() - t1
                break
    finally:
        await client.aclose()
    return res
