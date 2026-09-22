"""Camera control: protocol, transport and angles.

Turns director Shots into iRacing broadcast messages. The whole path is built and
tested on Linux behind the CameraController seam; only SdkCameraController (sdk.py)
is Win32-locked.

Nothing in here imports the director or the world model. The loops that run the
director over this live in show/live.py, and the shot vocabulary the angle policy
needs comes from show/contract.py, so `import pylon.camera` loads the
transport and nothing behind it (tests/test_camera.py checks that in a clean
interpreter).
"""

from __future__ import annotations

from .actuator import Actuator, ActuatorConfig, CameraMap
from .angles import POLICIES, Angle, AnglePolicy, AngleRotator
from .bridge import (
    BridgeClient,
    BridgeServer,
    CommandSink,
    DirectSink,
    WsCommandClient,
)
from .command import CamCommand, CameraState, CamOp, CsMode, RpyPosMode, RpySrchMode
from .controller import (
    CameraController,
    LoggingController,
    RecordingController,
    apply,
)

__all__ = [
    "POLICIES",
    "Actuator",
    "ActuatorConfig",
    "Angle",
    "AnglePolicy",
    "AngleRotator",
    "BridgeClient",
    "BridgeServer",
    "CamCommand",
    "CamOp",
    "CameraController",
    "CameraMap",
    "CameraState",
    "CommandSink",
    "CsMode",
    "DirectSink",
    "LoggingController",
    "RecordingController",
    "RpyPosMode",
    "RpySrchMode",
    "WsCommandClient",
    "apply",
]
