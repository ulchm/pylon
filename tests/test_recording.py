import math

from pylon.telemetry import PlaybackSource, RecordingWriter, SyntheticSource
from pylon.telemetry.frame import Frame
from pylon.telemetry.recording import RecordingReader


def test_record_playback_roundtrip(tmp_path):
    src = SyntheticSource(num_cars=8, duration_s=5.0, hz=10.0, seed=1)
    out = tmp_path / "rec.jsonl.gz"

    original = []
    with RecordingWriter(out, src.session_info(), sample_rate=10.0, source="test") as w:
        for fr in src.frames():
            w.write_frame(fr)
            original.append(fr)

    pb = PlaybackSource(str(out))
    read = list(pb.frames())

    assert len(read) == len(original) > 0
    for a, b in zip(original, read):
        assert a.tick == b.tick
        assert math.isclose(a.session_time, b.session_time, abs_tol=1e-3)
        # per-car arrays survive the round trip exactly
        assert a.get("CarIdxPosition") == b.get("CarIdxPosition")
        assert a.get("CarIdxLapDistPct") == b.get("CarIdxLapDistPct")
        assert a.get("SessionFlags") == b.get("SessionFlags")

    # session info (track + roster) is preserved in the header
    assert pb.session_info().track_name == src.session_info().track_name
    assert len(pb.session_info().drivers()) == 8


def test_an_unclosed_capture_is_readable_up_to_the_last_flush(tmp_path):
    """A capture is a long unattended job: a 6h endurance stint on the sim box. gzip
    buffers hard, so without periodic flushing the on-disk file holds nothing usable
    until close() writes the end-of-stream marker: a crash, a power blip, or the box
    rebooting used to lose the ENTIRE session, not just the tail.

    Reading a still-open capture is the same situation as reading a killed one, and
    it must give back everything up to the last flush."""
    src = SyntheticSource(num_cars=8, duration_s=60.0, hz=10.0, seed=3)
    out = tmp_path / "inflight.jsonl.gz"

    w = RecordingWriter(out, src.session_info(), sample_rate=10.0, source="test")
    w.open()
    try:
        for fr in src.frames():
            w.write_frame(fr)
        assert w.count > 300

        # nothing closed, nothing finalised: exactly what a killed process leaves behind
        pb = PlaybackSource(str(out))
        read = list(pb.frames())

        assert pb.truncated, "expected the reader to notice the missing end-of-stream marker"
        # everything up to the last flush boundary survives (all but the final partial batch)
        assert len(read) >= w.count - w.flush_every, \
            f"lost the session: only {len(read)} of {w.count} frames survived"
        assert [f.tick for f in read] == list(range(len(read)))   # a clean, contiguous prefix
        assert pb.session_info().track_name == src.session_info().track_name
        assert read[-1].get("CarIdxLapDistPct")   # last surviving frame is whole
    finally:
        w.close()

    # and closing normally still produces a complete, untruncated recording
    done = PlaybackSource(str(out))
    assert len(list(done.frames())) == w.count
    assert not done.truncated


def test_plain_and_gzip_both_work(tmp_path):
    src = SyntheticSource(num_cars=4, duration_s=2.0, hz=5.0, seed=2)
    for name in ("rec.jsonl", "rec.jsonl.gz"):
        out = tmp_path / name
        with RecordingWriter(out, src.session_info(), sample_rate=5.0) as w:
            for fr in src.frames():
                w.write_frame(fr)
        assert len(list(PlaybackSource(str(out)).frames())) == w.count


def _st_frame(tick: int, st: float, session_num: int = 2) -> Frame:
    return Frame(tick=tick, session_time=st,
                 values={"SessionTime": st, "SessionNum": session_num, "CarIdxLap": [1]})


def test_a_replay_excursion_is_not_written_into_the_capture(tmp_path):
    """A recording must stay monotonic. The frames served while the tape is elsewhere
    describe an earlier moment, and a capture whose clock folds back makes every
    offline replay of it reset derived state mid-race."""
    path = tmp_path / "excursion.jsonl.gz"
    with RecordingWriter(path, None, sample_rate=10.0) as w:
        for k in range(50):                       # live, 100.0 -> 104.9
            w.write_frame(_st_frame(k, 100.0 + k * 0.1))
        for k in range(30):                       # a replay seeks back to 70s
            w.write_frame(_st_frame(100 + k, 70.0 + k * 0.1))
        for k in range(50):                       # ...and comes home
            w.write_frame(_st_frame(200 + k, 105.0 + k * 0.1))
        skipped, excursions = w.skipped, w.excursions

    assert skipped == 30 and excursions == 1
    times = [f.session_time for f in RecordingReader(path).frames()]
    assert len(times) == 100
    assert times == sorted(times), "the capture's clock folds back on itself"
    assert min(times) >= 100.0


def test_a_rewind_we_never_return_from_is_adopted_not_discarded(tmp_path):
    """The 2026-07-25 failure: session time went 21358 -> 4742 and STAYED there. A
    naive high-water rule would have skipped the entire rest of the capture, which is
    far worse than the fold it was protecting against."""
    path = tmp_path / "rewind.jsonl.gz"
    with RecordingWriter(path, None, sample_rate=10.0) as w:
        for k in range(20):
            w.write_frame(_st_frame(k, 21358.0 + k * 0.1))
        for k in range(1500):                     # 150s on the new clock, never returning
            w.write_frame(_st_frame(100 + k, 4742.0 + k * 0.1))
        skipped = w.skipped

    times = [f.session_time for f in RecordingReader(path).frames()]
    # the excursion window is dropped, but the new timeline is adopted and kept
    assert skipped < 1000, f"threw away the rest of the capture ({skipped} frames)"
    assert len(times) > 600
    assert max(times) > 4880.0, "did not keep recording on the new clock"


def test_a_session_change_restarts_the_clock_without_skipping(tmp_path):
    """Practice -> qualifying legitimately restarts session time. That is a new
    session, not a scrub, and none of it may be dropped."""
    path = tmp_path / "sessions.jsonl.gz"
    with RecordingWriter(path, None, sample_rate=10.0) as w:
        for k in range(30):
            w.write_frame(_st_frame(k, 900.0 + k * 0.1, session_num=1))
        for k in range(30):
            w.write_frame(_st_frame(100 + k, 5.0 + k * 0.1, session_num=2))
        assert w.skipped == 0

    assert len(list(RecordingReader(path).frames())) == 60
