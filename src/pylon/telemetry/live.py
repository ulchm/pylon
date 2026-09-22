"""LiveSource + recorder: the ONE Win32-only component.

Reads the live iRacing shared memory via pyirsdk. This only works where iRacing
is running and pyirsdk can attach (Windows, or Python-under-Wine sharing the
sim's prefix). On Linux with no sim it simply reports "not connected". Everything
else in the package is platform-agnostic and testable without this.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator

from .frame import Frame, SessionInfo

# Channels we capture live. Broad on purpose (capture-once, never re-capture):
# every CarIdx array the director could want, plus session state.
DEFAULT_CHANNELS: list[str] = [
    "SessionTime", "SessionTick", "SessionNum", "SessionState", "SessionFlags",
    "SessionTimeRemain", "SessionLapsRemain", "SessionTimeOfDay",
    "CarIdxLapDistPct", "CarIdxLap", "CarIdxLapCompleted", "CarIdxPosition",
    "CarIdxClassPosition", "CarIdxClass", "CarIdxEstTime", "CarIdxF2Time",
    "CarIdxOnPitRoad", "CarIdxTrackSurface", "CarIdxTrackSurfaceMaterial",
    "CarIdxGear", "CarIdxRPM", "CarIdxLastLapTime", "CarIdxBestLapTime",
    "CarIdxSessionFlags", "CarIdxPaceFlags", "CarIdxPaceLine", "CarIdxPaceRow",
    # Conditions. Only the ones DESIGN.md section 14 found to be worth anything:
    # track temp is the single good dynamic signal, air temp is scene-setting, skies
    # and wetness are enums, precipitation is unvalidated (every capture is dry), and
    # wind is gusty enough that only a qualitative reading is honest. Density,
    # pressure and humidity are left out on purpose: every-frame noise.
    # NOTE: this list is what the bridge streams live; recordings enumerate every
    # channel, so a replay carries these whether or not they are listed here.
    "TrackTemp", "AirTemp", "Skies", "TrackWetness", "Precipitation", "WindVel",
    # Replay state. IsReplayPlaying is what tells every downstream consumer that the
    # frames arriving describe an EARLIER moment, not the live race: the overlay's
    # LIVE/REPLAY badge already reads it, and it has been silently False on every live
    # broadcast because it was never in this list (it only ever worked off recordings,
    # which enumerate every channel). ReplaySessionTime/Num confirm where a seek
    # actually landed, and the speed pair confirms slow motion took.
    #
    # ReplayFrameNum/ReplayFrameNumEnd are a PAIR, and it is their sum that means
    # something: the tape's total length, fixed on a saved replay and growing while a
    # session records. That is what tells a re-broadcast .rpy from a live race (#62,
    # DESIGN.md 14). This comment used to claim ReplayFrameNumEnd is 0 at the live edge
    #: it is not, it is 1, 19 or a flat 1201 on the Spa captures depending on where
    # the viewer parks, and building the badge on that claim badged live races REPLAY.
    "IsReplayPlaying", "ReplaySessionTime", "ReplaySessionNum",
    "ReplayFrameNum", "ReplayFrameNumEnd", "ReplayPlaySpeed", "ReplayPlaySlowMotion",
    # Where the camera ACTUALLY is, as against where the director asked it to go (#68).
    # Everything downstream used to render the director's intent and assume it became
    # reality; when the two diverged at Oulton Park the tower and the pop-ins described a
    # car that was not on screen for a long stretch, and nothing anywhere reported it.
    # These two are what make that divergence observable at all.
    #
    # NOTE the file's warning above: this list is what the SIM-BOX AGENT streams, so on
    # the two-box path these values only reach a live broadcast once that agent exe is
    # rebuilt and redeployed. Until then a live feed carries no camera channels, which
    # must read as "unknown" and never as "the camera is on car zero".
    "CamCarIdx", "CamGroupNumber",
]


class LiveSource:
    def __init__(self, poll_hz: float = 10.0, channels: Iterable[str] | None = None):
        import irsdk  # local import: irsdk is only meaningful in a Win32 context

        self._ir = irsdk.IRSDK()
        self.poll_hz = poll_hz
        self.dt = 1.0 / poll_hz
        self.channels = list(channels) if channels else list(DEFAULT_CHANNELS)

    def connect(self) -> bool:
        return bool(self._ir.startup())

    @property
    def connected(self) -> bool:
        return bool(self._ir.is_connected)

    def session_info(self) -> SessionInfo:
        raw = {
            "WeekendInfo": self._ir["WeekendInfo"] or {},
            "DriverInfo": self._ir["DriverInfo"] or {},
            # CameraInfo carries the TV1/TV2/TV3 group numbers the actuator needs.
            "CameraInfo": self._ir["CameraInfo"] or {},
            # SessionInfo is the weekend's schedule, and the ONLY place that says
            # whether the cars on track are practising, qualifying or racing:
            # WeekendInfo.EventType reads "Race" all weekend (DESIGN.md 14).
            # Sent whole. Its `ResultsPositions` tables are the bulk of it (16KB of
            # 17KB on a 28-car field, and they scale with the grid) but they are also
            # the only per-car FastestTime a finished session has: the qualifying
            # classification lives there, and CarIdxBestLapTime does not carry it
            # across a session boundary. ~50KB every 60s at VLN grid sizes, against
            # the 45KB of DriverInfo already going out: not worth trimming.
            "SessionInfo": self._ir["SessionInfo"] or {},
            # Where the sectors start, a few numbers. The only sector information the
            # sim publishes for anybody but the player, and what world/sectors.py
            # measures every car's splits against. Without it the whole quick-lap
            # programme (the director's hold on a shot) is silently off.
            "SplitTimeInfo": self._ir["SplitTimeInfo"] or {},
        }
        return SessionInfo(raw)

    def frames(self) -> Iterator[Frame]:
        tick = 0
        while self._ir.is_connected:
            self._ir.freeze_var_buffer_latest()
            values = {}
            for ch in self.channels:
                v = self._ir[ch]
                if v is not None:
                    values[ch] = v
            st = float(values.get("SessionTime") or 0.0)
            yield Frame(tick=tick, session_time=st, values=values)
            tick += 1
            time.sleep(self.dt)


def record(out_path: str, *, duration_s: float | None = None, hz: float = 10.0) -> int:
    """Capture live telemetry to a recording. Returns a process exit code."""
    src = LiveSource(poll_hz=hz)
    if not src.connect() or not src.connected:
        print(
            "iRacing not detected (no live shared memory).\n"
            "Run this on the Windows/Wine box with iRacing running (spectating a session)."
        )
        return 1

    from .recording import RecordingWriter

    info = src.session_info()
    print(
        f"connected: track={info.track_name} drivers={len(info.drivers())} "
        f"-> recording to {out_path}"
    )
    n = 0
    start = None
    with RecordingWriter(out_path, info, sample_rate=hz, source="live") as w:
        for fr in src.frames():
            w.write_frame(fr)
            n += 1
            if duration_s is not None:
                start = fr.session_time if start is None else start
                if fr.session_time - start >= duration_s:
                    break
    print(f"wrote {n} frames")
    return 0
