"""Overlay wiring: map WorldSnapshots to the timing-tower model and stream them.

`snapshot_to_model` is the seam between the world model and the browser overlay
(overlays/broadcast-overlay.html, whose script is overlays/overlay.js). identity.py
resolves names, classes and credentials from session info; model.py builds the model;
transport.py is the HTTP and WebSocket transport.

Everything runs on the one Windows rig: OBS loads the served page as a Browser Source
off localhost, and only data ever crosses a socket, never video.
"""


from __future__ import annotations

from .identity import class_meta, driver_meta
from .model import LocalDirector, ShotSource, TowerModel, snapshot_to_model
from .transport import PublishedShots, serve, serve_live

__all__ = [
    "LocalDirector",
    "PublishedShots",
    "ShotSource",
    "TowerModel",
    "class_meta",
    "driver_meta",
    "serve",
    "serve_live",
    "snapshot_to_model",
]
