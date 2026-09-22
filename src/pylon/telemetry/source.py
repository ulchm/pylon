"""The TelemetrySource interface every source implements.

Downstream code (world model, director, overlays) depends only on this, never on
whether the data is live, replayed, or synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from .frame import Frame, SessionInfo


@runtime_checkable
class TelemetrySource(Protocol):
    def session_info(self) -> SessionInfo:
        """Static-ish session context (track, driver roster)."""
        ...

    def frames(self) -> Iterator[Frame]:
        """Yield telemetry frames in order. May be finite (playback/synthetic) or
        run until disconnected (live)."""
        ...
