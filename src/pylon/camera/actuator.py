"""The actuator: translate a director Shot into a CamCommand.

This is the render-domain seam. The director decides *what* to watch (a Shot with
a target car index); the actuator decides *how* to point iRacing's TV director at
it: which car number, which camera group. Pure logic, no SDK, so it is tuned and
tested on Linux; only the final send (SdkCameraController) is Win32.

Two lookups make a Shot concrete:
- target_idx -> car number string, from the session DriverInfo.
- a logical group name (TV1/TV2/TV3) -> the real per-track group number, from
  CameraInfo.Groups (11/12/13 at Spa, but per-track, so never hardcode).

Which *angle* (in-car, chase, trackside, chopper) is decided by an AnglePolicy and
rotated for variety in angles.py; the actuator just resolves the chosen logical
group to a real per-track group number and lets iRacing pick the specific camera
within it (camera=0). Per-track lap-section camera maps are a further Phase 5 win.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..show.contract import ShotKind
from ..telemetry.frame import SessionInfo
from .angles import AnglePolicy, AngleRotator
from .command import CamCommand, CsMode

if TYPE_CHECKING:
    from ..director.model import Shot

DEFAULT_GROUP = 1  # iRacing's first group; last-resort fallback if no TV group exists


class CameraMap:
    """Resolve logical camera-group names to iRacing group numbers for a track."""

    def __init__(self, by_name: dict[str, int], default_group: int = DEFAULT_GROUP):
        self.by_name = by_name
        self.default_group = default_group

    @classmethod
    def from_session(cls, info: SessionInfo) -> CameraMap:
        by_name = {name: num for num, name in info.camera_groups() if name}
        # Prefer TV1 as the default; else the lowest-numbered TV group; else group 1.
        default = by_name.get("TV1")
        if default is None:
            tv = sorted(num for name, num in by_name.items() if name.startswith("TV"))
            default = tv[0] if tv else DEFAULT_GROUP
        return cls(by_name, default)

    def resolve(self, name: str) -> int:
        return self.by_name.get(name, self.default_group)

    def available(self, name: str) -> bool:
        """True when this track actually has a group by that logical name (so the
        angle policy can skip absent angles instead of silently defaulting)."""
        return name in self.by_name


@dataclass(frozen=True)
class ActuatorConfig:
    # camera-angle personality: what angles each shot kind may use, and their order
    # (resolved to real per-track group numbers by the CameraMap). See angles.py.
    policy: AnglePolicy = field(default_factory=AnglePolicy.classic)
    camera: int = 0  # 0 = let iRacing choose the camera within the group


class Actuator:
    def __init__(self, info: SessionInfo, cfg: ActuatorConfig | None = None,
                 camera_map: CameraMap | None = None):
        self.numbers = {idx: d.number for idx, d in info.drivers_by_idx().items()}
        self._pinned_map = camera_map is not None
        self.cmap = camera_map or CameraMap.from_session(info)
        self.cfg = cfg or ActuatorConfig()
        self._rotator = AngleRotator(self.cfg.policy)

    def refresh_info(self, info: SessionInfo) -> None:
        """Adopt fresh session info: a late joiner gets a number, a team swap keeps it.

        The numbers are the whole point (a car with no number cannot be cut to, and the
        director does not know that). The camera map is re-read too unless one was
        pinned by hand, and the angle rotator keeps its history, so a refresh never
        repeats an angle back to back.
        """
        self.numbers = {idx: d.number for idx, d in info.drivers_by_idx().items()}
        if not self._pinned_map:
            self.cmap = CameraMap.from_session(info)

    def command_for(self, shot: Shot, session_time: float | None = None) -> CamCommand | None:
        """Concrete camera command for a shot, or None if it can't be framed."""
        group = self._rotator.pick(self.cmap, shot.kind, shot.flavor)
        number = self.numbers.get(shot.target_idx)
        if number is not None:
            return CamCommand.switch_num(number, group, self.cfg.camera,
                                         label=shot.label, session_time=session_time)
        # No known car number (e.g. pace car / stale index): fall back to a
        # position-based special target so the shot still lands somewhere sane.
        if shot.kind == ShotKind.INCIDENT:
            return CamCommand.switch_pos(CsMode.AT_INCIDENT, group, self.cfg.camera,
                                         label=shot.label, session_time=session_time)
        if shot.kind in (ShotKind.LEADER, ShotKind.FOLLOW):
            return CamCommand.switch_pos(CsMode.AT_LEADER, group, self.cfg.camera,
                                         label=shot.label, session_time=session_time)
        return None
