"""PlaybackSource: replays a recording on any platform (no SDK, no Win32).

This is the primary dev vehicle for real captures. It is just RecordingReader
under the intended name.
"""

from __future__ import annotations

from .recording import RecordingReader


class PlaybackSource(RecordingReader):
    pass
