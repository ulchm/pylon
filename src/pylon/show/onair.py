"""Notice the moment OBS starts streaming, once.

The studio polls OBS's stream output every few seconds. The first answer is
the baseline, never an announcement: a studio restarted mid-race must not
ping the server again, and a stream started before the studio is the wrong
order anyway (the tower would not load). After that, every off-to-on edge is
one go-live. OBS being unreachable is not an edge in either direction; the
baseline simply waits for the first real answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class OnAir:
    def __init__(self, is_streaming: Callable[[], bool | None], on_live: Callable[[], None], *,
                 every: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self._probe, self._on_live = is_streaming, on_live
        self.every, self._clock = every, clock
        self._next = 0.0
        self.streaming: bool | None = None       # last known state; None until OBS answers
        self.went_live = 0                       # edges seen, for the dock

    def tick(self) -> bool:
        """Poll if due. True when this tick saw the stream start."""
        now = self._clock()
        if now < self._next:
            return False
        self._next = now + self.every
        state = self._probe()
        if state is None:
            return False
        was, self.streaming = self.streaming, state
        if was is False and state is True:
            self.went_live += 1
            self._on_live()
            return True
        return False


def obs_stream_probe(connect: Callable[[], object]) -> Callable[[], bool | None]:
    """A probe over obs-websocket that keeps its client between polls and
    drops it on any error, so OBS coming up later is simply the next answer."""
    holder: dict = {}

    def probe() -> bool | None:
        try:
            if "cl" not in holder:
                holder["cl"] = connect()
            return bool(holder["cl"].get_stream_status().output_active)
        except Exception:  # noqa: BLE001 - OBS down or not yet up: no answer, not an error
            holder.pop("cl", None)
            return None

    return probe
