"""Provision the OBS programme scene (PROGRAM_SCENE) over obs-websocket.

One scene = the sim's video full-frame, with our overlay browser source composited
on top, on a 1920x1080/60 canvas, ready to NVENC-encode out. The director drives
the *in-sim* cameras, so OBS mostly holds this one composite; scene cuts (replay
wipes, intermissions) are later polish.

The video is OBS's own game capture of the sim, on this same box: a texture copy,
not an encode and a decode. (Until 2026-09 there was a second path, NDI from a
separate sim box across the LAN. The whole show runs on the one Windows rig now, so
that path and its DistroAV dependency are gone.)

Idempotent: creates the scene and sources only if missing, so it is safe to
re-run, and re-running is the documented fix-up: game capture reports 0x0 until
it hooks the sim, so the crop only lands on a run made while the sim is up.

Input *kinds* are discovered at runtime (the capture kinds, the browser kind),
rather than hardcoded, so this survives plugin version changes.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..settings import SHOW
from .output import obs_dir


def _ws_config_path() -> Path:
    """Where obs-websocket keeps the password OBS generated for itself.

    OBS puts its plugin config under %APPDATA% on Windows and ~/.config elsewhere
    (`output.obs_dir`). Guessing wrong fails silently in the worst way:
    `default_password()` returns None, and `connect()` then dies on authentication
    with an error that blames the password rather than the path it was never read
    from.
    """
    return obs_dir() / "plugin_config/obs-websocket/config.json"


WS_CONFIG = _ws_config_path()
# The browser-source URLs, built in settings.py from the same ports the servers bind.
DEFAULT_OVERLAY_URL = SHOW.overlay_page_url()

GAME_SOURCE_NAME = "iRacing (Capture)"
OVERLAY_SOURCE_NAME = "Overlay"
CARDS_URL = SHOW.cards_url()
_WIDTH, _HEIGHT = SHOW.canvas
# The holding cards: what goes to air when there is nothing to show. Each gets
# its own scene holding one full-frame browser source and nothing else: no
# video, because the point is that the sim has nothing worth pointing at.
#
# (label, ?card= preset). The SCENE is named `{prefix}{label}` and the SOURCE
# inside it is named `Card - {label}`, so re-prefixing the scenes to group them
# in OBS's list does not rename the sources underneath them.
#
# The countdown target is set at showtime, not here: add ?in=<secs> or
# ?at=<time> to the source URL on the day. Without either, the clock line
# collapses and it is just a branded "Starting Soon".
CARDS = (
    ("Starting Soon", "soon"),
    ("Intermission", "intermission"),
    ("Race Complete", "complete"),
)
# OBS sorts its scene list alphabetically, so a common prefix keeps the cards
# together and away from the programme scene. The operator's show name is what
# normally supplies it (show/obssetup.py); this is the fallback for a show with
# no name set, because scenes called " - Starting Soon" look broken.
SCENE_PREFIX = "Pylon - "
PROGRAM_SCENE = "iRacing - Broadcast"


def card_scene_names(prefix: str = SCENE_PREFIX) -> list[str]:
    return [f"{prefix}{label}" for label, _ in CARDS]


def default_password() -> str | None:
    """The obs-websocket password OBS already generated, so no need to pass one."""
    try:
        return json.loads(WS_CONFIG.read_text()).get("server_password")
    except (OSError, ValueError):
        return None


def connect(host: str = SHOW.obs_host, port: int = SHOW.obs_port,
            password: str | None = None, timeout: float = 5.0):
    import logging

    import obsws_python as obs

    # obsws_python logs a refused connection with a full traceback and then raises it.
    # Every caller here catches that and prints one line saying what it means for the
    # show, so the traceback is duplicate noise on a studio console that is 30 lines
    # deep. A NullHandler stops Python's last-resort stderr handler printing it while
    # leaving any real logging configuration in charge.
    logging.getLogger("obsws_python").addHandler(logging.NullHandler())
    return obs.ReqClient(host=host, port=port,
                         password=password or default_password(), timeout=timeout)


def _find_kind(kinds: list[str], needle: str) -> str | None:
    return next((k for k in kinds if needle in k.lower()), None)


def _game_settings(window: str = "") -> dict:
    """Settings for the local capture source.

    With no window named, OBS's game capture takes any fullscreen app, which is the
    right default on a sim rig: the sim is the only thing running fullscreen. Naming
    a window ("title:class:exe") pins it instead, which is what you want if you
    spectate in a window, or if anything else on that desktop goes fullscreen.

    priority 2 is match-by-executable. Titles carry the session name and classes are
    an implementation detail, so the exe is the only part of that triple that stays
    put across sessions.
    """
    if window:
        return {"capture_mode": "window", "window": window, "priority": 2}
    return {"capture_mode": "any_fullscreen"}


def _crop_target(spec: str, width: int, height: int) -> tuple[int, int] | None:
    """Parse a crop spec into the size to keep. None means leave the source alone.

    "center" is the useful one: keep a canvas-sized window out of the middle. On a
    triple-wide rig the sim renders 3840x1080 across three screens and the broadcast
    wants the middle 1920x1080 of it, which is exactly a canvas-sized centre crop.
    """
    if not spec or spec == "none":
        return None
    if spec == "center":
        return width, height
    w, _, h = spec.partition("x")
    return int(w), int(h)


def _centered_crop(src_w: int, src_h: int, want_w: int, want_h: int) -> dict:
    """Crop edges that leave a `want`-sized window in the middle of the source.

    Never negative: a source SMALLER than the target is not cropped at all, and the
    scene item's bounds scale it up instead. Odd leftovers go to the right/bottom.
    """
    dw, dh = max(0, src_w - want_w), max(0, src_h - want_h)
    left, top = dw // 2, dh // 2
    return {"cropLeft": left, "cropRight": dw - left,
            "cropTop": top, "cropBottom": dh - top}


def _apply_capture_crop(cl, scene: str, source: str, want: tuple[int, int],
                        canvas: tuple[int, int], report: dict) -> None:
    """Centre-crop the capture to `want` and fit the result to the canvas.

    The source size is only knowable once the capture has actually hooked the sim:
    OBS reports 0x0 until then. So this re-applies on every run rather than trying to
    be clever: provision now, start the sim, run obs-setup again and the crop lands.

    Alignment 5 is LEFT|TOP, so the bounds box starts at the canvas origin; the bounds
    alignment stays 0 (centre) so anything that does not divide evenly is letterboxed
    rather than shoved into a corner.
    """
    try:
        item_id = cl.get_scene_item_id(scene, source).scene_item_id
        tr = cl.get_scene_item_transform(scene, item_id).scene_item_transform
    except Exception as e:  # noqa: BLE001 - a missing item must not sink provisioning
        report["warnings"].append(f"could not read the transform for '{source}', so it "
                                  f"was left uncropped: {e}")
        return

    src_w, src_h = int(tr.get("sourceWidth") or 0), int(tr.get("sourceHeight") or 0)
    if src_w <= 0 or src_h <= 0:
        report["warnings"].append(
            f"'{source}' has not hooked the sim yet, so its size is unknown and the crop "
            f"was skipped - start the sim and re-run obs-setup.")
        return

    crop = _centered_crop(src_w, src_h, *want)
    cl.set_scene_item_transform(scene, item_id, {
        **crop,
        "positionX": 0, "positionY": 0, "alignment": 5,
        "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0,
        "boundsWidth": canvas[0], "boundsHeight": canvas[1],
    })
    report["crop"] = {"source": [src_w, src_h], "kept": [want[0], want[1]], **crop}


def _create_input(cl, scene: str, name: str, kind: str, settings: dict,
                  warnings: list[str] | None = None) -> bool:
    """Create one input in `scene`, reporting rather than raising. True if it was made.

    OBS input names are unique across the WHOLE scene collection, not per scene, so a
    name another show already owns comes back as request error 601 and used to end the
    run in a traceback partway through, leaving a programme scene with nothing in it.
    That is not hypothetical: on the rig this was cut from, the other broadcaster's
    sources are called "Overlay" and "iRacing (Capture)" too.
    """
    try:
        cl.create_input(scene, name, kind, settings, True)
        return True
    except Exception as e:  # noqa: BLE001 - a named conflict beats a half-built OBS
        if warnings is not None:
            warnings.append(
                f"could not create '{name}' in '{scene}': {e}. Something else in this "
                f"scene collection already uses that name; give this show its own tag "
                f"in config.toml, or rename the other source in OBS.")
        return False


def ensure_cards(cl, *, base_url: str = CARDS_URL, prefix: str = SCENE_PREFIX,
                 source_prefix: str = "", warnings: list[str] | None = None,
                 width: int = _WIDTH, height: int = _HEIGHT) -> list[str]:
    """Create the holding scenes (idempotent). Returns the scene names made or kept.

    `shutdown` and `restart_when_active` are both on, which is what makes a
    `?in=<seconds>` countdown usable: the page reloads every time you cut to
    the scene, so the clock starts from the top instead of carrying on from
    wherever it got to the last time the card was up.
    """
    kinds = cl.get_input_kind_list(True).input_kinds
    browser_kind = _find_kind(kinds, "browser")
    if not browser_kind:
        return []

    existing = {s["sceneName"] for s in cl.get_scene_list().scenes}
    made = []
    for label, card in CARDS:
        scene = f"{prefix}{label}"
        if scene not in existing:
            cl.create_scene(scene)
        settings = {
            "url": f"{base_url}?card={card}",
            "width": width, "height": height,
            "shutdown": True, "restart_when_active": True,
        }
        # Named off the LABEL, not the scene, so re-prefixing scenes to reorder them
        # in OBS does not orphan the source inside each one. The show's tag is still
        # carried, because the scene names alone do not keep two shows apart: input
        # names are collection-wide, so an untagged "Card - Starting Soon" is one
        # object both shows would own.
        source = f"{source_prefix}Card - {label}"
        items = {i["sourceName"] for i in cl.get_scene_item_list(scene).scene_items}
        if source not in items:
            _create_input(cl, scene, source, browser_kind, settings, warnings)
        else:
            cl.set_input_settings(source, settings, True)
        made.append(scene)
    return made


STREAM_SERVICE = "rtmp_custom"


def ensure_stream_target(cl, *, server: str, key: str) -> dict:
    """Point OBS's stream output at `server` with `key`. Returns a report.

    Idempotent: reads what OBS has and writes only on a difference, because
    obs-websocket refuses to change the service while an output is running and
    a no-op re-run during a show should not turn into a warning. The key is
    never put in the report (the caller prints the report, and a secret in a
    console is a secret in a screenshot), only whether one is set.
    """
    report: dict = {"server": server, "key_set": bool(key), "changed": False, "warnings": []}
    if not key:
        report["warnings"].append(
            "stream server is set but the key is empty (PYLON_STREAM_KEY); OBS "
            "will connect and be refused.")
    try:
        cur = cl.get_stream_service_settings()
        settings = cur.stream_service_settings or {}
        if (cur.stream_service_type == STREAM_SERVICE
                and settings.get("server") == server and settings.get("key") == key):
            return report
        cl.set_stream_service_settings(STREAM_SERVICE,
                                       {"server": server, "key": key, "use_auth": False})
        report["changed"] = True
    except Exception as e:  # noqa: BLE001 - OBS refuses this while streaming
        report["warnings"].append(
            f"could not set the stream target ({e}); OBS refuses while a stream is "
            f"running, so stop it and run obs-setup again.")
    return report


def _on_air(cl) -> bool:
    """Whether OBS is streaming or recording right now.

    Asked before cutting the programme scene, because provisioning is something you
    might run on a PC that is mid-broadcast: this product was cut from a broadcaster
    that still runs on its author's rig, and switching the scene there puts a
    half-built programme on somebody else's air. Unknown counts as off air, so an OBS
    that cannot answer behaves the way it always did.
    """
    try:
        return bool(cl.get_stream_status().output_active
                    or cl.get_record_status().output_active)
    except Exception:  # noqa: BLE001 - cannot tell, so behave as before
        return False


def build_program_scene(cl, *, scene: str = PROGRAM_SCENE,
                        overlay_url: str = DEFAULT_OVERLAY_URL,
                        game_window: str = "",
                        capture_crop: str = "center",
                        source_prefix: str = "",
                        width: int = _WIDTH, height: int = _HEIGHT,
                        fps: int = SHOW.fps) -> dict:
    """Create/refresh the Program scene. Returns a small report + warnings.

    `capture_crop` is "center", "none" or "WxH"; see `_crop_target`.

    `source_prefix` is the show's tag, and it goes on the SOURCES as well as the
    scene, because OBS input names are unique per scene collection rather than per
    scene. Empty (the single-broadcaster default) leaves the plain names.
    """
    report: dict = {"scene": scene, "video": None, "game": None,
                    "overlay": None, "crop": None, "warnings": []}
    game_source = f"{source_prefix}{GAME_SOURCE_NAME}"
    overlay_source = f"{source_prefix}{OVERLAY_SOURCE_NAME}"

    try:
        cl.set_video_settings(fps, 1, width, height, width, height)
    except Exception as e:  # noqa: BLE001 - OBS refuses this while an output is active
        # The documented fix-up is "start the sim, run obs-setup again", and a
        # second run once the stream is already up must still apply the crop.
        report["warnings"].append(
            f"could not set the canvas to {width}x{height}@{fps} ({e}); OBS refuses "
            f"while a stream or recording is running, so the rest still applies.")

    scenes = {s["sceneName"] for s in cl.get_scene_list().scenes}
    if scene not in scenes:
        cl.create_scene(scene)
    items = {i["sourceName"] for i in cl.get_scene_item_list(scene).scene_items}

    kinds = cl.get_input_kind_list(True).input_kinds
    game_kind = _find_kind(kinds, "game_capture") or _find_kind(kinds, "window_capture")
    browser_kind = _find_kind(kinds, "browser")

    # Video first, so it sits at the bottom of the stack; the overlay goes on top.
    if game_kind:
        report["video"] = report["game"] = game_kind
        if game_source not in items:
            _create_input(cl, scene, game_source, game_kind,
                          _game_settings(game_window), report["warnings"])
        if not game_window:
            report["warnings"].append(
                "Game capture is on any-fullscreen - fine while the sim owns the screen, "
                "but name the window (or re-run with --game-window) if you spectate "
                "in a window.")
        # Re-applied every run, not just on create: the source size is unknown until
        # the capture hooks the sim, so re-running once it is up is the fix-up path.
        try:
            want = _crop_target(capture_crop, width, height)
        except ValueError:
            want = None
            report["warnings"].append(
                f"capture-crop {capture_crop!r} is not 'center', 'none' or WxH - "
                f"leaving the capture uncropped.")
        if want:
            _apply_capture_crop(cl, scene, game_source, want, (width, height), report)
    else:
        report["warnings"].append(
            "No game or window capture kind found - is this OBS running on Windows, "
            "on the sim rig?")

    if browser_kind:
        report["overlay"] = overlay_url
        overlay_settings = {"url": overlay_url, "width": width, "height": height}
        if overlay_source not in items:
            _create_input(cl, scene, overlay_source, browser_kind, overlay_settings,
                          report["warnings"])
        else:
            # Re-applied on every run, like the crop: the URL is built from the ports
            # in config.toml, and "change the config and run obs-setup again" is the
            # documented way to apply a change. A source left on the old port loads
            # nothing, which looks like a broken overlay rather than a stale setting.
            cl.set_input_settings(overlay_source, overlay_settings, True)
    else:
        report["warnings"].append("No browser input kind found - is the browser plugin loaded?")

    if _on_air(cl):
        report["warnings"].append(
            "OBS is streaming or recording, so the programme scene was left where it "
            "is; cut to this scene by hand once the current show is off air.")
    else:
        cl.set_current_program_scene(scene)
    return report
