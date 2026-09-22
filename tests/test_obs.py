"""OBS scene provisioning, tested against a fake obs-websocket client.

build_program_scene() is pure request-issuing logic, so a recording fake stands in
for OBS: we assert it creates the scene, adds the sim capture then the overlay on
top (order matters for stacking), sets the program scene, and is idempotent.
"""

import sys

from pylon.obs.setup import (
    CARDS,
    GAME_SOURCE_NAME,
    OVERLAY_SOURCE_NAME,
    _ws_config_path,
    build_program_scene,
    card_scene_names,
    ensure_cards,
    ensure_stream_target,
)


class _Resp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeClient:
    def __init__(self, scenes=(), kinds=("game_capture", "browser_source"), items=(),
                 source_size=(1920, 1080)):
        self._scenes = list(scenes)
        self._kinds = list(kinds)
        self._items = list(items)
        self._source_size = source_size  # what the capture reports; 0x0 until it hooks
        self.calls: list = []

    def set_video_settings(self, *a):
        self.calls.append(("set_video_settings", *a))

    def get_scene_list(self):
        return _Resp(scenes=[{"sceneName": s} for s in self._scenes])

    def create_scene(self, name):
        self.calls.append(("create_scene", name))
        self._scenes.append(name)

    def get_scene_item_list(self, name):
        return _Resp(scene_items=[{"sourceName": i} for i in self._items])

    def get_input_kind_list(self, unversioned):
        return _Resp(input_kinds=self._kinds)

    def create_input(self, scene, name, kind, settings, enabled):
        self.calls.append(("create_input", scene, name, kind, settings))
        self._items.append(name)
        return _Resp(scene_item_id=len(self.calls))

    def set_current_program_scene(self, name):
        self.calls.append(("set_current_program_scene", name))

    def set_input_audio_monitor_type(self, name, mon_type):
        self.calls.append(("set_input_audio_monitor_type", name, mon_type))

    def set_input_settings(self, name, settings, overlay):
        self.calls.append(("set_input_settings", name, settings))

    def get_scene_item_id(self, scene, source, offset=None):
        return _Resp(scene_item_id=7)

    def get_scene_item_transform(self, scene, item_id):
        w, h = self._source_size
        return _Resp(scene_item_transform={"sourceWidth": w, "sourceHeight": h})

    def set_scene_item_transform(self, scene, item_id, transform):
        self.calls.append(("set_scene_item_transform", scene, item_id, transform))

    # stream service: what OBS has, as (type, settings); starts as OBS ships
    stream = ("rtmp_common", {"service": "Twitch", "key": ""})

    def get_stream_service_settings(self):
        t, ss = self.stream
        return _Resp(stream_service_type=t, stream_service_settings=dict(ss))

    def set_stream_service_settings(self, ss_type, ss_settings):
        self.calls.append(("set_stream_service_settings", ss_type, ss_settings))
        self.stream = (ss_type, dict(ss_settings))


def _ops(fake):
    return [c[0] for c in fake.calls]


def test_builds_program_scene_video_then_overlay():
    fake = FakeClient()
    report = build_program_scene(fake, scene="Program",
                                 game_window="iRacing.com Simulator:x:iRacingSim64DX11.exe")

    assert _ops(fake) == [
        "set_video_settings", "create_scene",
        "create_input", "set_scene_item_transform",   # the capture, then its crop
        "create_input", "set_current_program_scene",
    ]
    # canvas is 1920x1080@60
    assert fake.calls[0] == ("set_video_settings", 60, 1, 1920, 1080, 1920, 1080)
    # recorded call = (op, scene, name, kind, settings)
    # capture created first (bottom), overlay second (on top)
    game_call, overlay_call = [c for c in fake.calls if c[0] == "create_input"]
    assert game_call[2] == GAME_SOURCE_NAME and game_call[3] == "game_capture"
    assert overlay_call[2] == OVERLAY_SOURCE_NAME and overlay_call[4]["url"].startswith("http")
    assert fake.calls[-1] == ("set_current_program_scene", "Program")
    assert report["game"] == "game_capture" and report["overlay"].startswith("http")
    assert not report["warnings"]


def test_idempotent_when_sources_exist():
    fake = FakeClient(scenes=["Program"],
                      items=[GAME_SOURCE_NAME, OVERLAY_SOURCE_NAME])
    build_program_scene(fake, scene="Program")
    # scene + sources already present: no create_scene, no create_input
    assert "create_scene" not in _ops(fake)
    assert "create_input" not in _ops(fake)
    assert ("set_current_program_scene", "Program") in fake.calls


def test_missing_plugins_are_reported():
    fake = FakeClient(kinds=["color_source", "image_source"])  # no capture, no browser
    report = build_program_scene(fake)
    assert report["game"] is None and report["overlay"] is None
    assert any("capture" in w for w in report["warnings"])
    assert any("browser" in w for w in report["warnings"])


def test_cards_create_one_scene_each_and_reload_on_activate():
    fake = FakeClient()
    made = ensure_cards(fake, base_url="http://x/cards.html")

    assert made == card_scene_names()
    assert _ops(fake) == ["create_scene", "create_input"] * len(CARDS)

    # each scene gets its own card, and the URL carries the preset
    urls = [c[4]["url"] for c in fake.calls if c[0] == "create_input"]
    assert urls == [f"http://x/cards.html?card={card}" for _, card in CARDS]

    # a ?in= countdown is only usable if the page reloads on every cut TO the
    # scene: otherwise the clock resumes wherever it left off last time
    for c in (c for c in fake.calls if c[0] == "create_input"):
        assert c[4]["shutdown"] and c[4]["restart_when_active"]


def test_cards_are_idempotent():
    fake = FakeClient(scenes=card_scene_names(),
                      items=[f"Card - {label}" for label, _ in CARDS])
    ensure_cards(fake)

    assert "create_scene" not in _ops(fake)
    assert "create_input" not in _ops(fake)
    assert _ops(fake) == ["set_input_settings"] * len(CARDS)


def test_card_sources_are_named_off_the_label_not_the_scene():
    """Re-prefixing the scenes to group them in OBS must not orphan the browser
    source inside each one."""
    fake = FakeClient()
    ensure_cards(fake, prefix="Holding / ")

    scenes = [c[1] for c in fake.calls if c[0] == "create_scene"]
    sources = [c[2] for c in fake.calls if c[0] == "create_input"]
    assert scenes == [f"Holding / {label}" for label, _ in CARDS]
    assert sources == [f"Card - {label}" for label, _ in CARDS]


# --- video source: OBS's own capture of the sim, on this box ---

_WIN_KINDS = ("game_capture", "window_capture", "browser_source")


def test_the_video_is_a_game_capture_of_the_sim():
    """OBS runs on the sim rig, so the picture is a texture copy of the sim's own
    window: no encode, no decode, no second box."""
    fake = FakeClient(kinds=_WIN_KINDS)
    report = build_program_scene(fake, scene="Program")

    made = [c for c in fake.calls if c[0] == "create_input"]
    assert made[0][2] == GAME_SOURCE_NAME  # video first: bottom of the stack
    assert made[0][3] == "game_capture"
    assert made[0][4] == {"capture_mode": "any_fullscreen"}
    assert report["game"] == "game_capture" and report["video"] == "game_capture"


def test_a_named_window_is_pinned_by_executable():
    """Titles carry the session name and classes are an implementation detail, so the
    exe (priority 2) is the only part of the triple that survives a new session."""
    fake = FakeClient(kinds=_WIN_KINDS)
    report = build_program_scene(fake, game_window="iRacing.com Simulator:x:iRacingSim64DX11.exe")

    call = next(c for c in fake.calls if c[0] == "create_input" and c[2] == GAME_SOURCE_NAME)
    assert call[4] == {"capture_mode": "window",
                       "window": "iRacing.com Simulator:x:iRacingSim64DX11.exe",
                       "priority": 2}
    assert not report["warnings"]  # a pinned window needs no nagging


def test_any_fullscreen_warns_because_it_grabs_whatever_is_fullscreen():
    fake = FakeClient(kinds=_WIN_KINDS)
    report = build_program_scene(fake)
    assert any("any-fullscreen" in w for w in report["warnings"])


def test_an_obs_without_a_capture_kind_says_so():
    fake = FakeClient(kinds=("browser_source",))  # a Linux OBS: no game capture at all
    report = build_program_scene(fake)

    assert report["video"] is None
    assert GAME_SOURCE_NAME not in [c[2] for c in fake.calls if c[0] == "create_input"]
    assert any("Windows" in w for w in report["warnings"])


def test_window_capture_stands_in_when_game_capture_is_missing():
    fake = FakeClient(kinds=("window_capture", "browser_source"))
    report = build_program_scene(fake)
    assert report["game"] == "window_capture"


def test_capture_source_is_idempotent():
    fake = FakeClient(scenes=["Program"], kinds=_WIN_KINDS,
                      items=[GAME_SOURCE_NAME, OVERLAY_SOURCE_NAME])
    build_program_scene(fake, scene="Program", game_window="x:y:z")
    assert "create_input" not in _ops(fake)


def _crop_of(fake):
    return next(c[3] for c in fake.calls if c[0] == "set_scene_item_transform")


def test_triple_wide_is_cropped_to_the_middle_of_the_canvas():
    """The sim renders 3840x1080 across three screens; the broadcast wants the middle
    1920x1080 of it, which is 960 off each side and nothing off top or bottom."""
    fake = FakeClient(kinds=_WIN_KINDS, source_size=(3840, 1080))
    report = build_program_scene(fake, game_window="x:y:iRacingSim64DX11.exe")

    t = _crop_of(fake)
    assert (t["cropLeft"], t["cropRight"]) == (960, 960)
    assert (t["cropTop"], t["cropBottom"]) == (0, 0)
    # the kept region fills the canvas from its origin, not from its centre
    assert (t["positionX"], t["positionY"], t["alignment"]) == (0, 0, 5)
    assert (t["boundsWidth"], t["boundsHeight"]) == (1920, 1080)
    assert report["crop"]["source"] == [3840, 1080] and report["crop"]["kept"] == [1920, 1080]


def test_crop_is_skipped_until_the_capture_hooks_the_sim():
    """OBS reports 0x0 for a game capture that has not attached yet. Cropping off that
    would take the whole frame away, so it says so and waits to be re-run."""
    fake = FakeClient(kinds=_WIN_KINDS, source_size=(0, 0))
    report = build_program_scene(fake, game_window="x:y:z")

    assert "set_scene_item_transform" not in _ops(fake)
    assert report["crop"] is None
    assert any("has not hooked the sim yet" in w for w in report["warnings"])


def test_crop_none_leaves_the_capture_alone():
    fake = FakeClient(kinds=_WIN_KINDS, source_size=(3840, 1080))
    report = build_program_scene(fake, game_window="x:y:z", capture_crop="none")
    assert "set_scene_item_transform" not in _ops(fake)
    assert report["crop"] is None


def test_an_explicit_crop_size_is_honoured():
    fake = FakeClient(kinds=_WIN_KINDS, source_size=(3840, 1080))
    build_program_scene(fake, game_window="x:y:z", capture_crop="2560x1080")
    t = _crop_of(fake)
    assert (t["cropLeft"], t["cropRight"]) == (640, 640)


def test_an_odd_leftover_goes_to_the_right():
    fake = FakeClient(kinds=_WIN_KINDS, source_size=(3841, 1080))
    build_program_scene(fake, game_window="x:y:z")
    t = _crop_of(fake)
    assert (t["cropLeft"], t["cropRight"]) == (960, 961)  # 1921 to lose, not 1920


def test_a_source_smaller_than_the_canvas_is_scaled_not_cropped_negative():
    """One 1280x1080 screen into a 1920x1080 canvas: a negative crop would be nonsense,
    so the bounds scale it up instead."""
    fake = FakeClient(kinds=_WIN_KINDS, source_size=(1280, 1080))
    build_program_scene(fake, game_window="x:y:z")
    t = _crop_of(fake)
    assert (t["cropLeft"], t["cropRight"], t["cropTop"], t["cropBottom"]) == (0, 0, 0, 0)
    assert t["boundsType"] == "OBS_BOUNDS_SCALE_INNER"


def test_a_nonsense_crop_spec_warns_rather_than_exploding():
    fake = FakeClient(kinds=_WIN_KINDS, source_size=(3840, 1080))
    report = build_program_scene(fake, game_window="x:y:z", capture_crop="wide-ish")
    assert "set_scene_item_transform" not in _ops(fake)
    assert any("not 'center', 'none' or WxH" in w for w in report["warnings"])


def test_ws_config_path_follows_the_platform(monkeypatch):
    """The failure this guards is silent: read the wrong path and default_password()
    returns None, then connect() dies on auth blaming the password."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\fully\AppData\Roaming")
    win = _ws_config_path()
    assert str(win).startswith(r"C:\Users\fully\AppData\Roaming")
    assert win.name == "config.json" and "obs-websocket" in win.as_posix()

    monkeypatch.setattr(sys, "platform", "linux")
    assert ".config/obs-studio" in _ws_config_path().as_posix()


def test_a_refused_canvas_setting_warns_and_provisions_the_rest():
    """obs-websocket refuses video settings while a stream or recording is running,
    and the documented fix-up is a second run once the sim is up, which may well be
    after the stream has started. The crop must still land."""
    class Busy(FakeClient):
        def set_video_settings(self, *a):
            raise RuntimeError("an output is active")

    fake = Busy(kinds=_WIN_KINDS)
    report = build_program_scene(fake, game_window="x:y:z")
    assert report["game"] == "game_capture"
    assert any("canvas" in w for w in report["warnings"])


# ---------------------------------------------------------------- stream target
_RS = "rtmp://192.168.1.248:1935/live"
_KEY = "6aa1f10f.stream?token=secret"


def test_stream_target_is_set_as_a_custom_rtmp_service():
    fake = FakeClient()
    report = ensure_stream_target(fake, server=_RS, key=_KEY)
    assert report["changed"] and report["key_set"] and not report["warnings"]
    assert fake.stream == ("rtmp_custom", {"server": _RS, "key": _KEY, "use_auth": False})


def test_stream_target_is_a_no_op_when_obs_already_has_it():
    """Re-running obs-setup during a show must not touch the service: OBS refuses
    the write while streaming, and a needless write would read as a failure."""
    fake = FakeClient()
    fake.stream = ("rtmp_custom", {"server": _RS, "key": _KEY, "use_auth": False})
    report = ensure_stream_target(fake, server=_RS, key=_KEY)
    assert not report["changed"] and not report["warnings"]
    assert "set_stream_service_settings" not in _ops(fake)


def test_stream_target_report_never_carries_the_key():
    report = ensure_stream_target(FakeClient(), server=_RS, key=_KEY)
    assert _KEY not in repr(report)


def test_an_empty_key_is_a_warning_not_a_silent_misconfiguration():
    report = ensure_stream_target(FakeClient(), server=_RS, key="")
    assert report["changed"] and not report["key_set"]
    assert any("key is empty" in w for w in report["warnings"])


def test_a_refused_stream_target_warns_and_says_why():
    class Streaming(FakeClient):
        def set_stream_service_settings(self, *a):
            raise RuntimeError("an output is active")

    report = ensure_stream_target(Streaming(), server=_RS, key=_KEY)
    assert not report["changed"]
    assert any("stop it and run obs-setup again" in w for w in report["warnings"])
