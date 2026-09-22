"""OBS control (Phase 4, production side): provision the broadcast scene.

Runs on the sim rig, which is also the production switcher + encoder. Talks to the
local OBS over obs-websocket (obsws-python). Needs only the browser-source plugin:
the scene holds OBS's own game capture of the sim with our overlay composited on
top.
"""

from __future__ import annotations

from .output import OutputSettings, ensure_output
from .scenes import ObsSwitcher, SceneConfig, SceneDirector
from .setup import (
    PROGRAM_SCENE,
    build_program_scene,
    connect,
    default_password,
    ensure_cards,
    ensure_stream_target,
)

__all__ = ["PROGRAM_SCENE", "ObsSwitcher", "OutputSettings", "SceneConfig", "SceneDirector",
           "build_program_scene", "connect", "default_password", "ensure_cards",
           "ensure_output", "ensure_stream_target"]
