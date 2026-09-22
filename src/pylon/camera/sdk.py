"""SdkCameraController: the ONE Win32-locked piece of the camera path.

Sends real iRacing broadcast messages via pyirsdk. These are Win32 window
messages (RegisterWindowMessageW + SendNotifyMessageW to HWND_BROADCAST, per
DESIGN.md section 13), so they only work where iRacing is running and this
process shares its desktop/wineprefix. Everything upstream (actuator, command
protocol, bridge transport) is platform-agnostic and does not import this.

Instantiating attaches to the live sim (like LiveSource). Pass an existing
`ir` (an irsdk.IRSDK) to reuse a connection or to inject a fake in tests.
"""

from __future__ import annotations


class SdkCameraController:
    def __init__(self, ir=None):
        if ir is None:
            import irsdk  # local import: only meaningful in a Win32 context

            ir = irsdk.IRSDK()
            ir.startup()
        self._ir = ir

    @property
    def connected(self) -> bool:
        return bool(getattr(self._ir, "is_connected", False))

    def switch_num(self, car_number: str, group: int, camera: int = 0) -> None:
        self._ir.cam_switch_num(str(car_number), group, camera)

    def switch_pos(self, position: int, group: int, camera: int = 0) -> None:
        self._ir.cam_switch_pos(position, group, camera)

    def set_state(self, state: int) -> None:
        self._ir.cam_set_state(state)

    # --- replay transport (DESIGN.md section 7) ---------------------------
    # Same HWND_BROADCAST mechanism as the camera ops, so this is transport, not a
    # new channel. Only the sim can seek its own tape, which is why replays run
    # through here rather than an OBS output buffer.
    def replay_seek(self, session_num: int, session_time_ms: int) -> None:
        self._ir.replay_search_session_time(int(session_num), int(session_time_ms))

    def replay_search(self, search_mode: int) -> None:
        self._ir.replay_search(int(search_mode))

    def replay_speed(self, speed: int, slow_motion: bool = False) -> None:
        self._ir.replay_set_play_speed(int(speed), bool(slow_motion))

    def replay_pos(self, pos_mode: int, frame_num: int = 0) -> None:
        self._ir.replay_set_play_position(int(pos_mode), int(frame_num))

    # --- diagnostic -------------------------------------------------------
    def raw_broadcast(self, msg: int, var1: int, var2: int, var3: int) -> dict:
        """Send one arbitrary broadcast message and REPORT WHAT WIN32 SAID.

        The whole replay path is fire-and-forget: pyirsdk returns SendNotifyMessageW's
        result and every wrapper throws it away, so "the sim ignored it" and "the call
        never succeeded" are indistinguishable from the Linux side. This is the only
        thing that tells them apart, which is why it also reports GetLastError.

        `ret == 0` means Windows refused to post the message (look at `err`; 5 is
        ACCESS_DENIED, the signature of UIPI blocking a lower-integrity process from
        messaging an elevated one). `ret != 0` means it was delivered and the sim chose
        to do nothing with it.
        """
        import ctypes

        ctypes.windll.kernel32.SetLastError(0)
        ret = self._ir._broadcast_msg(int(msg), int(var1), int(var2), int(var3))
        err = ctypes.windll.kernel32.GetLastError()
        return {"ret": int(ret or 0), "err": int(err), "msg": int(msg),
                "vars": [int(var1), int(var2), int(var3)],
                "connected": bool(getattr(self._ir, "is_connected", False))}
