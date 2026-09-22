"""CamCommand: the platform-agnostic camera-control wire protocol.

A `CamCommand` is one iRacing broadcast message, described as pure data so it can
be built on Linux (by the actuator), serialized over the LAN bridge, and finally
turned into a real `pyirsdk` broadcast call on the Win32 box (by SdkCameraController).
Nothing here touches the SDK, so it is fully testable off the sim box.

Constants (CsMode / CameraState / the op set) are quoted from the verified API
surface in DESIGN.md section 13.
"""

from __future__ import annotations

from dataclasses import dataclass


class CamOp:
    SWITCH_NUM = "switch_num"  # follow car #N with a camera group
    SWITCH_POS = "switch_pos"  # follow whoever is running Pth (or a CsMode target)
    SET_STATE = "set_state"    # live/replay, hide UI, auto-shot on/off
    # --- replay transport (DESIGN.md section 7: replays run through the SIM's replay
    # system, not an OBS output buffer, because only the sim can show action the live
    # camera missed). Same HWND_BROADCAST mechanism as the camera ops above.
    REPLAY_SEEK = "replay_seek"      # jump to a session time (THE primary verb)
    REPLAY_SEARCH = "replay_search"  # to_start/to_end/prev_lap/prev_incident/...
    REPLAY_SPEED = "replay_speed"    # play/pause/rewind, with native slow motion
    REPLAY_POS = "replay_pos"        # absolute frame positioning (begin/current/end + offset)
    # Deliberately NOT wrapped: replay_set_state's only mode is erase_tape, which wipes
    # the replay buffer. There is no broadcast reason to ever send it and every reason
    # not to have it one typo away from a live show.
    RAW = "raw"  # DIAGNOSTIC ONLY: send an arbitrary broadcast message, report the
                 # Win32 return value. Exists because the sim accepts camera messages
                 # and silently ignores replay ones, and every hypothesis about why
                 # (argument packing, whether the call even succeeds, whether NON-camera
                 # messages work at all) otherwise costs a hand redeploy of the sim-box
                 # agent to test. One deploy, unlimited experiments. See RAW_FORBIDDEN.


# Broadcast types the raw diagnostic will not send, whatever it is asked to do.
# erase_tape destroys the replay buffer; pit commands act on a real car in a real
# session. Neither is ever a thing we want to discover by accident at 2am.
RAW_FORBIDDEN = frozenset({6, 9})   # replay_set_state, pit_command


class RpySrchMode:
    """replay_search: relative moves along the replay tape (DESIGN.md section 13)."""

    TO_START = 0
    TO_END = 1          # back to the live edge: how a replay RETURNS to live
    PREV_SESSION = 2
    NEXT_SESSION = 3
    PREV_LAP = 4
    NEXT_LAP = 5
    PREV_FRAME = 6
    NEXT_FRAME = 7
    PREV_INCIDENT = 8   # iRacing's OWN incident markers: an independent cross-check
    NEXT_INCIDENT = 9   # on our telemetry-derived incident detection


class RpyPosMode:
    """replay_set_play_position: what `frame_num` is measured from."""

    BEGIN = 0
    CURRENT = 1
    END = 2


class CsMode:
    """Special camera targets, passed as the `position` of a switch_pos."""

    AT_INCIDENT = -3
    AT_LEADER = -2
    AT_EXCITING = -1


class CameraState:
    """Flags OR'd together for a set_state command (DESIGN.md section 13)."""

    IS_SESSION_SCREEN = 0x0001
    IS_SCENIC_ACTIVE = 0x0002
    CAM_TOOL_ACTIVE = 0x0004
    UI_HIDDEN = 0x0008
    USE_AUTO_SHOT_SELECTION = 0x0010
    USE_TEMPORARY_EDITS = 0x0020
    USE_KEY_ACCELERATION = 0x0040
    USE_KEY10X_ACCELERATION = 0x0080
    USE_MOUSE_AIM_MODE = 0x0100


@dataclass(frozen=True)
class CamCommand:
    """One broadcast message. Only the fields relevant to `op` are populated."""

    op: str
    car_number: str | None = None   # switch_num: target car number, as a STRING
    position: int | None = None     # switch_pos: running position, or a CsMode.*
    group: int | None = None        # camera group number (per-track; see CameraMap)
    camera: int = 0                 # 0 = let iRacing pick a camera within the group
    state: int | None = None        # set_state: CameraState flags OR'd together
    label: str = ""                 # human-readable, for logs (not sent to the SDK)
    session_time: float | None = None  # when the director chose this shot

    # replay fields. Note session_time above is metadata (when we DECIDED); the seek's
    # target instant is session_time_ms, in the sim's own units, and they are not the
    # same number: a replay exists precisely because it points at an earlier moment.
    session_num: int | None = None      # replay_seek: which session of the weekend
    session_time_ms: int | None = None  # replay_seek: the instant to jump to
    search_mode: int | None = None      # replay_search: an RpySrchMode
    speed: int | None = None            # replay_speed: 0 paused, 1 play, -N rewind
    slow_motion: bool = False           # replay_speed: divide by `speed` instead of multiply
    pos_mode: int | None = None         # replay_pos: an RpyPosMode
    frame_num: int | None = None        # replay_pos: frames from pos_mode's origin
    # DIAGNOSTIC (CamOp.RAW): [broadcast_type, var1, var2, var3], sent verbatim.
    # Its own field rather than borrowed replay ones, because the whole point is that
    # what goes on the wire is exactly what was asked for.
    raw_args: tuple[int, int, int, int] | None = None

    # --- constructors -----------------------------------------------------
    @classmethod
    def switch_num(cls, car_number: str, group: int, camera: int = 0, *,
                   label: str = "", session_time: float | None = None) -> CamCommand:
        return cls(CamOp.SWITCH_NUM, car_number=str(car_number), group=group,
                   camera=camera, label=label, session_time=session_time)

    @classmethod
    def switch_pos(cls, position: int, group: int, camera: int = 0, *,
                   label: str = "", session_time: float | None = None) -> CamCommand:
        return cls(CamOp.SWITCH_POS, position=position, group=group,
                   camera=camera, label=label, session_time=session_time)

    @classmethod
    def set_state(cls, state: int, *, label: str = "") -> CamCommand:
        return cls(CamOp.SET_STATE, state=state, label=label)

    @classmethod
    def raw(cls, msg: int, var1: int = 0, var2: int = 0, var3: int = 0, *,
            label: str = "") -> CamCommand:
        """DIAGNOSTIC. One arbitrary broadcast message, packed exactly as asked, so a
        packing hypothesis can be tested against the sim without a redeploy."""
        return cls(CamOp.RAW, raw_args=(int(msg), int(var1), int(var2), int(var3)),
                   label=label)

    @classmethod
    def replay_seek(cls, session_num: int, session_time_s: float, *,
                    label: str = "") -> CamCommand:
        """Jump the replay to a session time. Takes SECONDS, because that is what the
        director marks (WorldSnapshot.session_time); the milliseconds the SDK wants are
        this protocol's business, converted here so the rounding lives in one place."""
        return cls(CamOp.REPLAY_SEEK, session_num=int(session_num),
                   session_time_ms=round(session_time_s * 1000.0), label=label)

    @classmethod
    def replay_search(cls, search_mode: int, *, label: str = "") -> CamCommand:
        return cls(CamOp.REPLAY_SEARCH, search_mode=int(search_mode), label=label)

    @classmethod
    def replay_speed(cls, speed: int, *, slow_motion: bool = False,
                     label: str = "") -> CamCommand:
        """speed 1 = real time, 0 = pause, negative = rewind. With slow_motion the sim
        DIVIDES by speed, so (2, slow_motion=True) is half speed, not double."""
        return cls(CamOp.REPLAY_SPEED, speed=int(speed), slow_motion=bool(slow_motion),
                   label=label)

    @classmethod
    def replay_pos(cls, pos_mode: int, frame_num: int = 0, *, label: str = "") -> CamCommand:
        return cls(CamOp.REPLAY_POS, pos_mode=int(pos_mode), frame_num=int(frame_num),
                   label=label)

    # --- serialization ----------------------------------------------------
    def to_dict(self) -> dict:
        d: dict = {"op": self.op}
        if self.car_number is not None:
            d["car_number"] = self.car_number
        if self.position is not None:
            d["position"] = self.position
        if self.group is not None:
            d["group"] = self.group
        if self.op in (CamOp.SWITCH_NUM, CamOp.SWITCH_POS):
            d["camera"] = self.camera
        if self.state is not None:
            d["state"] = self.state
        if self.session_num is not None:
            d["session_num"] = self.session_num
        if self.session_time_ms is not None:
            d["session_time_ms"] = self.session_time_ms
        if self.search_mode is not None:
            d["search_mode"] = self.search_mode
        if self.speed is not None:
            d["speed"] = self.speed
            d["slow_motion"] = self.slow_motion
        if self.pos_mode is not None:
            d["pos_mode"] = self.pos_mode
            d["frame_num"] = self.frame_num
        if self.raw_args is not None:
            d["raw_args"] = list(self.raw_args)
        if self.label:
            d["label"] = self.label
        if self.session_time is not None:
            d["session_time"] = self.session_time
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CamCommand:
        return cls(
            op=d["op"],
            car_number=d.get("car_number"),
            position=d.get("position"),
            group=d.get("group"),
            camera=d.get("camera", 0),
            state=d.get("state"),
            label=d.get("label", ""),
            session_time=d.get("session_time"),
            session_num=d.get("session_num"),
            session_time_ms=d.get("session_time_ms"),
            search_mode=d.get("search_mode"),
            speed=d.get("speed"),
            slow_motion=bool(d.get("slow_motion", False)),
            pos_mode=d.get("pos_mode"),
            frame_num=d.get("frame_num"),
            raw_args=(tuple(d["raw_args"]) if d.get("raw_args") is not None else None),
        )
