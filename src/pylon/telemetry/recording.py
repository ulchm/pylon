"""Our own recording format: gzip-compressed JSON Lines.

Line 1 is a header (format/version, sample rate, captured channel list, and the
full session-info YAML dict). Every subsequent line is one frame:
    {"t": <tick>, "st": <session_time>, "v": {channel: value, ...}}

This is deliberately a faithful dump of whatever channels the source emitted, so
it never needs re-capturing when our interpretation changes. It is written by the
Win32 recorder (live capture) and by `synth`; it is read by PlaybackSource.
"""

from __future__ import annotations

import gzip
import json
import time
import zlib
from collections.abc import Iterable, Iterator
from typing import Any, Self

from .frame import Frame, SessionInfo

FORMAT = "pylon-rec"
VERSION = 1

# Flush this often while writing. A capture is a long-running unattended job (a 6h
# endurance stint on the sim box), and gzip buffers hard: without periodic flushing a
# crash, a power blip, or a hard kill leaves a file that raises EOFError on the FIRST
# read and takes the whole session with it. Z_SYNC_FLUSH (what GzipFile.flush does by
# default) ends a deflate block so everything written so far decompresses on its own.
FLUSH_EVERY = 100

# A recording must stay MONOTONIC in session time. Two things drive the clock
# backwards on a live sim: an instant replay seeking the tape (#16-#20), and a human
# scrubbing on the sim box: the latter happened for real on 2026-07-25 and is what
# issue #41 was about. Either way the frames served during the excursion describe an
# earlier moment, and writing them into a capture produces a file whose clock folds
# back on itself. Anything replaying that file then hits the fold and resets its
# derived state mid-race, which quietly ruins the offline tuning the recording exists
# for. So an excursion is SKIPPED, not recorded.
EXCURSION_BACK = 0.5      # seconds behind the high-water mark before it is an excursion
                          # (matches world.builder.TIME_JUMP_BACK; telemetry does not
                          # import world, so the value is repeated rather than shared)
# ...but a rewind we never come back from is not an excursion, it is the session having
# genuinely restarted, and skipping the rest of the capture would be far worse than the
# fold. On 2026-07-25 the clock went 21358 -> 4742 and simply stayed there; a naive
# high-water rule would have thrown away everything after it.
EXCURSION_MAX_SPAN = 90.0  # seconds of NEW-timeline progress before we accept it


def _open_write(path: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8")
    return open(path, "w", encoding="utf-8")


def _open_read(path: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


class RecordingWriter:
    def __init__(
        self,
        path: str,
        session_info: SessionInfo | dict | None,
        *,
        sample_rate: float = 60.0,
        source: str = "unknown",
        channels: Iterable[str] | None = None,
        flush_every: int = FLUSH_EVERY,
    ):
        self.path = str(path)
        self.flush_every = flush_every
        if isinstance(session_info, SessionInfo):
            self._session_info = session_info.raw
        else:
            self._session_info = session_info or {}
        self.sample_rate = sample_rate
        self.source = source
        self.channels = list(channels) if channels else None
        self.count = 0
        self.skipped = 0          # frames dropped as part of a replay/scrub excursion
        self.excursions = 0
        self._f = None
        self._max_st: float | None = None      # high-water session time written
        self._session_num: Any = None
        self._excursion_from: float | None = None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        self._f = _open_write(self.path)
        header = {
            "format": FORMAT,
            "version": VERSION,
            "source": self.source,
            "created": time.time(),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "session_info": self._session_info,
        }
        self._f.write(json.dumps(header, separators=(",", ":")) + "\n")

    def _is_excursion(self, frame: Frame) -> bool:
        """Is this frame part of a backwards excursion we should not record?"""
        st = frame.session_time
        num = frame.values.get("SessionNum")
        if num != self._session_num:
            # A new session of the weekend legitimately restarts the clock.
            self._session_num = num
            self._max_st = None
            self._excursion_from = None
        if self._max_st is None or st >= self._max_st - EXCURSION_BACK:
            if self._excursion_from is not None:
                self._excursion_from = None      # came home; resume recording
            self._max_st = st if self._max_st is None else max(self._max_st, st)
            return False
        if self._excursion_from is None:
            self._excursion_from = st
            self.excursions += 1
        elif st - self._excursion_from > EXCURSION_MAX_SPAN:
            # It has been running forward on the new clock long enough to be the real
            # timeline, not a detour. Adopt it rather than lose the rest of the capture.
            self._max_st = st
            self._excursion_from = None
            return False
        return True

    def write_frame(self, frame: Frame) -> None:
        if self._is_excursion(frame):
            self.skipped += 1
            return
        vals = frame.values
        if self.channels is not None:
            vals = {k: vals[k] for k in self.channels if k in vals}
        else:
            vals = dict(vals)
        line = {"t": frame.tick, "st": round(frame.session_time, 4), "v": vals}
        self._f.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.count += 1
        if self.flush_every and self.count % self.flush_every == 0:
            self._f.flush()  # keep the on-disk file readable if we're killed mid-capture

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
        if self.skipped:
            print(f"recording: skipped {self.skipped} frames across {self.excursions} "
                  f"replay/scrub excursion(s) to keep the capture monotonic")


class RecordingReader:
    """Reads a recording. Implements the TelemetrySource protocol.

    Tolerates a truncated file. A capture that was killed mid-write (crash, power
    loss, the sim box rebooting) still holds every frame up to the last flush, and
    hours of real multi-car telemetry is far too valuable to throw away over a
    missing end-of-stream marker. Reading one sets `truncated`.
    """

    def __init__(self, path: str):
        self.path = str(path)
        self.truncated = False
        with _open_read(self.path) as f:
            first = f.readline()
        if not first:
            raise ValueError(f"empty recording: {path}")
        self.header: dict[str, Any] = json.loads(first)

    def session_info(self) -> SessionInfo:
        return SessionInfo(self.header.get("session_info", {}))

    @property
    def sample_rate(self) -> float | None:
        return self.header.get("sample_rate")

    @property
    def channels(self) -> list[str] | None:
        return self.header.get("channels")

    def frames(self) -> Iterator[Frame]:
        self.truncated = False
        with _open_read(self.path) as f:
            f.readline()  # skip header
            while True:
                try:
                    line = f.readline()
                except (EOFError, gzip.BadGzipFile, zlib.error, UnicodeDecodeError):
                    # the compressed stream stops mid-block: everything before this
                    # point is good, so hand it back rather than losing the session.
                    self.truncated = True
                    return
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    self.truncated = True   # a half-written final line
                    return
                yield Frame(tick=obj["t"], session_time=obj["st"], values=obj["v"])
