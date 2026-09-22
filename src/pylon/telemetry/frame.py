"""Frame and session-info: the low-level, source-agnostic telemetry view.

A `Frame` is a faithful snapshot of whatever channels a source emitted, with no
interpretation. Turning frames into a per-car world model (gaps, battles, events)
happens in a later layer, on top of this. Keeping this layer dumb is deliberate:
the recording format then can't be "wrong", it just stores what the SDK produced,
so when our interpretation is wrong we fix code without re-capturing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Frame:
    tick: int
    session_time: float
    values: Mapping[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def car(self, key: str, idx: int, default: Any = None) -> Any:
        """Value of a per-car (CarIdx*) channel for one car index."""
        arr = self.values.get(key)
        if arr is None:
            return default
        try:
            return arr[idx]
        except (IndexError, TypeError):
            return default

    def has(self, key: str) -> bool:
        return key in self.values


@dataclass(frozen=True)
class Driver:
    car_idx: int
    number: str
    name: str | None = None
    #: `UserID`, iRacing's customer id. The only thing in a session string that
    #: identifies a PERSON rather than an entry: the name beside it arrives with
    #: duplicate-name digits welded on ("Bayle2"), the car number is per-session,
    #: and CarIdx is per-grid-slot. It is therefore what the league feed
    #: (`league/`) joins a championship row to a car on track. None for AI, and
    #: absent from an offline field, where there is nobody to look up anyway.
    cust_id: int | None = None
    car: str | None = None
    car_path: str | None = None  # CarPath, iRacing's stable internal id (e.g. "porsche963gtp")
    class_id: int | None = None
    class_name: str | None = None
    class_color: int | None = None  # CarClassColor, an 0xRRGGBB int (for the overlay)
    is_ai: bool = False
    is_pace_car: bool = False
    # Broadcast credentials. All three are absent (None) unless the session is a real
    # official one: an offline/AI field reports IRating 0 and LicString "R 0.00" for
    # everyone, so we normalise those placeholders away rather than putting "0" on air.
    irating: int | None = None
    lic_string: str | None = None  # "A 3.15": licence class letter + safety rating
    country: str | None = None     # FlairName, iRacing's per-driver flag (NOT ClubName)


def _as_int(v: Any) -> int | None:
    """CarClassColor comes through as an int (e.g. 0xffffff) or a numeric string."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# iRacing spells "this field is empty" several ways in DriverInfo, and they are strings,
# not nulls: ClubName is the literal "None" for every driver even in an official session,
# and the pace car's FlairName is "-none-".
_ABSENT = {"", "none", "-none-", "null", "undefined"}


def _clean_str(v: Any) -> str | None:
    text = str(v).strip() if v is not None else ""
    return text or None if text.lower() not in _ABSENT else None


def _clean_licence(v: Any) -> str | None:
    """LicString, unless it is the "no licence" placeholder every AI and the pace car
    carry. A real rookie sitting at exactly 0.00 safety rating is indistinguishable from
    the placeholder, and showing nothing beats showing a fake licence either way."""
    text = _clean_str(v)
    return None if text is None or text.upper() in {"R 0.00", "R 0.0"} else text


def _parse_length_m(text: str | None) -> float | None:
    if not text:
        return None
    m = re.match(r"\s*([\d.]+)\s*(km|m)\b", str(text))
    if not m:
        return None
    value = float(m.group(1))
    return value * 1000.0 if m.group(2) == "km" else value


class SessionInfo:
    """Wraps the iRacing session-info YAML (as a dict) with convenience accessors.

    Live, recording, and synthetic sources all produce the same `raw` shape
    (WeekendInfo / DriverInfo), so there is one accessor path for all of them.
    """

    def __init__(self, raw: dict | None):
        self.raw = raw or {}

    @property
    def weekend(self) -> dict:
        return self.raw.get("WeekendInfo", {}) or {}

    @property
    def track_name(self) -> str | None:
        """The track, named the way a broadcast names it.

        iRacing carries both spellings and the SHORT one is the broadcast one:
        `TrackDisplayShortName` is "Spa" where `TrackDisplayName` is "Circuit de
        Spa-Francorchamps" (verified on recordings/capture2). The long form does not
        fit a timing tower's header at any readable size (it needs ~280px against
        the header's ~190), so it was being ellipsised to "CIRCUIT DE SPA-FRANCORC…",
        which is worse than the real name the sport uses out loud.

        Falls back to the long name, then to None: a track with no short name spelled
        out is better ellipsised than blank.
        """
        return (_clean_str(self.weekend.get("TrackDisplayShortName"))
                or _clean_str(self.weekend.get("TrackDisplayName")))

    @property
    def track_config(self) -> str | None:
        """Which layout of the track is loaded: "Grand Prix", "Classic Boot".

        `WeekendInfo.TrackConfigName`, verified on recordings/capture2 where it
        reads "Grand Prix" against a `TrackDisplayShortName` of "Spa". The short
        name alone does not identify a lap (the Glen with the Inner Loop and the
        Glen without it share it), so anything that makes a claim about the
        corners needs this too.

        None on a track with no separate configurations, and None is the answer
        that must be safe: a consumer that cannot read the layout has to say
        nothing about it rather than assume the usual one.
        """
        return _clean_str(self.weekend.get("TrackConfigName"))

    def track_length_m(self) -> float | None:
        return _parse_length_m(self.weekend.get("TrackLength"))

    @property
    def sessions(self) -> list[dict]:
        """The weekend's schedule: SessionInfo.Sessions, one entry per session.

        Real shape (both Spa captures): three entries numbered 0/1/2, carrying
        SessionType "Practice" / "Lone Qualify" / "Race" and SessionName
        "PRACTICE" / "QUALIFY" / "RACE".
        """
        return self.raw.get("SessionInfo", {}).get("Sessions", []) or []

    def current_session_num(self) -> int | None:
        """SessionInfo.CurrentSessionNum: which entry the sim says is running.

        Second choice only. The frame's own `SessionNum` channel is per-tick truth,
        while this rides the session-info YAML, which the live overlay re-reads at
        most every 60s, so it can lag a session rolling over by a minute.
        """
        num = self.raw.get("SessionInfo", {}).get("CurrentSessionNum")
        return int(num) if isinstance(num, (int, float)) and not isinstance(num, bool) else None

    def session(self, num: int | None = None) -> dict:
        """One entry from `sessions`, by number; the current one when num is None."""
        if num is None:
            num = self.current_session_num()
        if num is None:
            return {}
        for s in self.sessions:
            if s.get("SessionNum") == num:
                return s
        return {}

    def session_type(self, num: int | None = None) -> str | None:
        """iRacing's SessionType for one session: "Practice", "Lone Qualify", "Race".

        This is NOT `WeekendInfo.EventType`. EventType describes the whole EVENT and
        reads "Race" for every session of a race weekend: verified on the two Spa
        captures, where `capture` IS the practice session (SessionNum 0) and still
        reports EventType "Race". Anything that needs to know whether the cars on
        screen are practising, qualifying or racing has to come through here.
        """
        return _clean_str(self.session(num).get("SessionType"))

    def drivers(self, include_pace_car: bool = False) -> list[Driver]:
        out: list[Driver] = []
        for d in self.raw.get("DriverInfo", {}).get("Drivers", []) or []:
            is_pace = bool(d.get("CarIsPaceCar"))
            if is_pace and not include_pace_car:
                continue
            out.append(
                Driver(
                    car_idx=d.get("CarIdx"),
                    number=str(d.get("CarNumber", "")),
                    name=d.get("UserName"),
                    # 0 is iRacing's "nobody", same as its absence.
                    cust_id=(_as_int(d.get("UserID")) or None),
                    car=d.get("CarScreenNameShort"),
                    car_path=d.get("CarPath"),
                    class_id=d.get("CarClassID"),
                    class_name=d.get("CarClassShortName") or d.get("CarClassName"),
                    class_color=_as_int(d.get("CarClassColor")),
                    is_ai=bool(d.get("CarIsAI")),
                    is_pace_car=is_pace,
                    irating=(_as_int(d.get("IRating")) or None),
                    lic_string=_clean_licence(d.get("LicString")),
                    country=_clean_str(d.get("FlairName")),
                )
            )
        return out

    def drivers_by_idx(self) -> dict[int, Driver]:
        return {d.car_idx: d for d in self.drivers()}

    def camera_groups(self) -> list[tuple[int, str]]:
        """(group number, group name) pairs from CameraInfo.Groups.

        The broadcast cameras are named TV1/TV2/TV3; their group numbers are
        per-track (11/12/13 at Spa, but never assume), so the actuator resolves
        logical names through this rather than hardcoding numbers.
        """
        out: list[tuple[int, str]] = []
        for g in self.raw.get("CameraInfo", {}).get("Groups", []) or []:
            num = g.get("GroupNum")
            if num is None:
                continue
            name = g.get("GroupName")
            out.append((int(num), str(name) if name is not None else ""))
        return out
