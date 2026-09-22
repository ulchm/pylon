"""CameraController: the seam between a CamCommand and a real camera cut.

`apply(controller, cmd)` decomposes a CamCommand into method calls. Swapping the
controller swaps what "cut" means:
- SdkCameraController (sdk.py, Win32) sends the actual iRacing broadcast message.
- RecordingController captures calls for assertions (tests / the LAN bridge on Linux).
- LoggingController prints them (dry runs, `pylon bridge --log`).

Because the director/actuator/bridge only ever see this protocol, the entire
pipeline runs and is validated on Linux; only SdkCameraController is Win32-locked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .command import RAW_FORBIDDEN, CamCommand, CamOp


@runtime_checkable
class CameraController(Protocol):
    def switch_num(self, car_number: str, group: int, camera: int = 0) -> None: ...
    def switch_pos(self, position: int, group: int, camera: int = 0) -> None: ...
    def set_state(self, state: int) -> None: ...
    # replay transport (DESIGN.md section 7)
    def replay_seek(self, session_num: int, session_time_ms: int) -> None: ...
    def replay_search(self, search_mode: int) -> None: ...
    def replay_speed(self, speed: int, slow_motion: bool = False) -> None: ...
    def replay_pos(self, pos_mode: int, frame_num: int = 0) -> None: ...
    def raw_broadcast(self, msg: int, var1: int, var2: int, var3: int) -> dict: ...


def apply(controller: CameraController, cmd: CamCommand):
    """Dispatch one CamCommand to a controller.

    Raises on an op the protocol does not define, that is a programming error and
    should be loud. It is NOT the same case as a sim-box agent too old to know a new
    op: that arrives over the wire, and BridgeServer catches it there so a forward-dated
    command can never drop a live telemetry stream.
    """
    if cmd.op == CamOp.SWITCH_NUM:
        controller.switch_num(cmd.car_number, cmd.group, cmd.camera)
    elif cmd.op == CamOp.SWITCH_POS:
        controller.switch_pos(cmd.position, cmd.group, cmd.camera)
    elif cmd.op == CamOp.SET_STATE:
        controller.set_state(cmd.state)
    elif cmd.op == CamOp.REPLAY_SEEK:
        controller.replay_seek(cmd.session_num, cmd.session_time_ms)
    elif cmd.op == CamOp.REPLAY_SEARCH:
        controller.replay_search(cmd.search_mode)
    elif cmd.op == CamOp.REPLAY_SPEED:
        controller.replay_speed(cmd.speed, cmd.slow_motion)
    elif cmd.op == CamOp.REPLAY_POS:
        controller.replay_pos(cmd.pos_mode, cmd.frame_num)
    elif cmd.op == CamOp.RAW:
        msg, v1, v2, v3 = cmd.raw_args
        if msg in RAW_FORBIDDEN:
            raise ValueError(f"raw broadcast {msg} is forbidden (erase-tape / pit command)")
        return controller.raw_broadcast(msg, v1, v2, v3)
    else:
        raise ValueError(f"unknown camera op: {cmd.op!r}")


@dataclass
class RecordingController:
    """Records every call as the CamCommand it corresponds to (test/dev sink)."""

    calls: list[CamCommand] = field(default_factory=list)

    def switch_num(self, car_number: str, group: int, camera: int = 0) -> None:
        self.calls.append(CamCommand.switch_num(car_number, group, camera))

    def switch_pos(self, position: int, group: int, camera: int = 0) -> None:
        self.calls.append(CamCommand.switch_pos(position, group, camera))

    def set_state(self, state: int) -> None:
        self.calls.append(CamCommand.set_state(state))

    def replay_seek(self, session_num: int, session_time_ms: int) -> None:
        self.calls.append(CamCommand.replay_seek(session_num, session_time_ms / 1000.0))

    def replay_search(self, search_mode: int) -> None:
        self.calls.append(CamCommand.replay_search(search_mode))

    def replay_speed(self, speed: int, slow_motion: bool = False) -> None:
        self.calls.append(CamCommand.replay_speed(speed, slow_motion=slow_motion))

    def replay_pos(self, pos_mode: int, frame_num: int = 0) -> None:
        self.calls.append(CamCommand.replay_pos(pos_mode, frame_num))

    def raw_broadcast(self, msg: int, var1: int, var2: int, var3: int) -> dict:
        self.calls.append(CamCommand.raw(msg, var1, var2, var3))
        return {"ret": 1, "err": 0, "fake": True}


class LoggingController:
    """Prints each call. Safe anywhere; the default for `pylon bridge`."""

    def switch_num(self, car_number: str, group: int, camera: int = 0) -> None:
        print(f"cam switch_num  car #{car_number}  group {group}  camera {camera}")

    def switch_pos(self, position: int, group: int, camera: int = 0) -> None:
        print(f"cam switch_pos  pos {position}  group {group}  camera {camera}")

    def set_state(self, state: int) -> None:
        print(f"cam set_state   0x{state:04x}")

    def replay_seek(self, session_num: int, session_time_ms: int) -> None:
        print(f"rpy seek        session {session_num}  t={session_time_ms / 1000.0:.1f}s")

    def replay_search(self, search_mode: int) -> None:
        print(f"rpy search      mode {search_mode}")

    def replay_speed(self, speed: int, slow_motion: bool = False) -> None:
        print(f"rpy speed       {speed}{'  (slow-mo)' if slow_motion else ''}")

    def replay_pos(self, pos_mode: int, frame_num: int = 0) -> None:
        print(f"rpy pos         mode {pos_mode}  frame {frame_num}")

    def raw_broadcast(self, msg: int, var1: int, var2: int, var3: int) -> dict:
        print(f"raw broadcast   msg {msg}  vars {var1},{var2},{var3}")
        return {"ret": 1, "err": 0, "fake": True}
