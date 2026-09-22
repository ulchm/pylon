"""Pin OBS's stream output: the encode that leaves the rig, stated once, in code.

Why this exists: Round 1 (2026-09-20) went out with a keyframe every 4.17s.
OBS's Simple output mode leaves the keyframe interval on "auto" (250 frames),
and until 2026-09-14 that did not matter, because the stream service was
"YouTube - RTMPS" and OBS quietly applies a known service's encoder rules
(keyframe 2s among them) when one is selected. Pointing OBS at the Restreamer
as a Custom server switched those rules off, nobody noticed, and YouTube
(whose ingest spec says "do not exceed 4 seconds" and which re-encodes every
stream) macroblocked all afternoon while Twitch, which hands viewers the
original bits, was fine. The Restreamer sends both platforms the same bytes
(8107 MB vs 8100 MB on the night), so the encode is the only lever.

So the output is provisioned like the stream target is: every value explicit,
in Advanced output mode, re-applied by `pylon obs-setup` and reported when
it differs. OBS keeps the settings in two files in the profile directory:

  * `basic.ini`: the output mode, which encoder, the audio track. Settable
    over obs-websocket while OBS runs (that is OBS's memory, written to disk on
    a CLEAN exit) and written straight to disk as well, because a force-killed
    OBS never writes its memory out and the rig has been force-killed before.
  * `streamEncoder.json`: the encoder's own parameters. No websocket API;
    the file is the interface. OBS reads it when it starts.

Both are read at startup, so a change here takes effect when OBS next starts,
and the report says so. Restart OBS before opening Settings > Output: the
dialog writes OBS's in-memory values back on OK, and until a restart those are
the old ones.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def obs_dir() -> Path:
    """OBS's config root: %APPDATA%\\obs-studio on Windows, ~/.config elsewhere."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    else:
        base = Path.home() / ".config"
    return base / "obs-studio"


NVENC_H264 = "obs_nvenc_h264_tex"


@dataclass(frozen=True)
class OutputSettings:
    """The encode. Defaults are the 2026-09-21 decision for the GOW show on the
    RTX 5070 Ti rig, made against Round 1's evidence:

    8000 kbps CBR: YouTube wants 4.5-9 Mbps for 1080p60 and re-encodes whatever
    it gets, so a fatter source survives that; 8000 is where Twitch's ingest is
    known to be happy for a non-partner channel (its documented ceiling is 6000,
    9000 is where people start reporting trouble). The house line is gigabit.
    Keyframe 2s: both platforms' spec. p6, full-res multipass, B-ref middle:
    quality per bit the GPU has to spare at 1080p60.
    """

    bitrate: int = 8000            # kbps, CBR
    keyint_sec: int = 2
    preset: str = "p6"             # NVENC p1 (fastest) .. p7 (best)
    audio_bitrate: int = 160       # kbps, AAC
    encoder: str = NVENC_H264

    def encoder_settings(self) -> dict:
        """streamEncoder.json: obs-nvenc's own keys, as plugins/obs-nvenc/
        nvenc-properties.c names them. Verified against OBS 32 on the rig by a
        recording probe: a wrong key is silently ignored and the default stays
        (the first cut wrote "preset2" and "b_ref_mode" and got p5 and B-ref off)."""
        return {
            "rate_control": "CBR",
            "bitrate": self.bitrate,
            "keyint_sec": self.keyint_sec,
            "preset": self.preset,
            "tune": "hq",
            "multipass": "fullres",
            "profile": "high",
            "lookahead": True,
            "adaptive_quantization": True,
            "bf": 2,
            "bframe_ref_mode": 2,     # NV_ENC_BFRAME_REF_MODE_MIDDLE
        }

    def profile_settings(self) -> dict[tuple[str, str], str]:
        """basic.ini: (section, key) -> value. Only what makes the encoder above
        the one on air; everything else in the profile is left as OBS has it."""
        return {
            ("Output", "Mode"): "Advanced",
            ("AdvOut", "Encoder"): self.encoder,
            ("AdvOut", "TrackIndex"): "1",
            ("AdvOut", "AudioEncoder"): "ffmpeg_aac",
            ("AdvOut", "Track1Bitrate"): str(self.audio_bitrate),
        }

    def describe(self) -> str:
        codec = "NVENC H.264" if self.encoder == NVENC_H264 else self.encoder
        return (f"{codec} CBR {self.bitrate} kbps, keyframe {self.keyint_sec}s, "
                f"{self.preset} full-res multipass, AAC {self.audio_bitrate}k")


def profile_dir(cl=None, base: Path | None = None) -> Path | None:
    """The current profile's directory. OBS records it in user.ini (global.ini
    before OBS 31); failing both, the profile name from obs-websocket, which is
    the directory name for any profile whose name is filesystem-safe."""
    base = base or obs_dir()
    for ini in ("user.ini", "global.ini"):
        name = ini_value(_read(base / ini), "Basic", "ProfileDir")
        if name:
            return base / "basic/profiles" / name
    if cl is not None:
        try:
            return base / "basic/profiles" / cl.get_profile_list().current_profile_name
        except Exception:  # noqa: BLE001 - no profile is a report, not a crash
            return None
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def ini_value(text: str, section: str, key: str) -> str | None:
    """One value from OBS's ini text, or None. OBS writes `key=value` with no
    spaces and no comments; this reads exactly that and nothing cleverer."""
    here = False
    for line in text.splitlines():
        if line.startswith("["):
            here = line.strip() == f"[{section}]"
        elif here and line.startswith(f"{key}="):
            return line[len(key) + 1:]
    return None


def ini_with(text: str, values: dict[tuple[str, str], str]) -> str:
    """OBS's ini text with `values` set, everything else byte-for-byte as it
    was. A key is replaced in place; a new one goes at the end of its section;
    a missing section is appended. Not configparser: that would re-serialise
    the whole file, and a profile OBS wrote should come back looking like one
    OBS wrote."""
    lines = text.splitlines()
    pending = dict(values)
    out: list[str] = []
    section = None

    def close_section():
        # keys for the section just finished that were not found: add them
        for (sec, key), val in list(pending.items()):
            if sec == section:
                # keep the blank line OBS leaves between sections after our keys
                while out and out[-1] == "":
                    out.pop()
                out.append(f"{key}={val}")
                out.append("")
                del pending[(sec, key)]

    for line in lines:
        if line.startswith("["):
            close_section()
            section = line.strip()[1:-1]
            out.append(line)
            continue
        if section is not None and "=" in line:
            key = line.split("=", 1)[0]
            if (section, key) in pending:
                out.append(f"{key}={pending.pop((section, key))}")
                continue
        out.append(line)
    close_section()
    for (sec, key), val in pending.items():
        if out and out[-1] != "":
            out.append("")
        out.append(f"[{sec}]")
        out.append(f"{key}={val}")
        section = sec
    return "\n".join(out) + ("\n" if out else "")


def ensure_output(cl, want: OutputSettings | None = None, *,
                  base: Path | None = None) -> dict:
    """Make OBS's stream output `want`. `cl` may be None when OBS is not running:
    the files are then the whole job, and the most reliable time to do it.

    Report: `changed` names what was written, `restart` says OBS has to be
    restarted for it to apply, `warnings` say what could not be done."""
    want = want or OutputSettings()
    report: dict = {"settings": want.describe(), "changed": [], "restart": False,
                    "warnings": []}
    pdir = profile_dir(cl, base)
    if pdir is None:
        report["warnings"].append(
            "could not find OBS's profile directory; output settings not written.")
        return report

    # basic.ini: OBS's memory over the websocket, and the disk copy underneath it.
    wanted = want.profile_settings()
    if cl is not None:
        for (sec, key), val in wanted.items():
            try:
                cur = cl.get_profile_parameter(sec, key).parameter_value
                if cur != val:
                    cl.set_profile_parameter(sec, key, val)
                    report["changed"].append(f"{sec}/{key}={val}")
            except Exception as e:  # noqa: BLE001 - one warning, keep going
                report["warnings"].append(f"could not set {sec}/{key} over the websocket ({e})")
                break
    ini_path = pdir / "basic.ini"
    text = _read(ini_path)
    if not text:
        report["warnings"].append(f"{ini_path} is missing or unreadable; output mode not pinned.")
    else:
        new = ini_with(text, wanted)
        if new != text and _write(ini_path, new, report, bom=True):
            for (sec, key), val in wanted.items():
                entry = f"{sec}/{key}={val}"
                if ini_value(text, sec, key) != val and entry not in report["changed"]:
                    report["changed"].append(entry)

    # streamEncoder.json: the encoder's parameters, merged over whatever is there.
    enc_path = pdir / "streamEncoder.json"
    try:
        current = json.loads(_read(enc_path) or "{}")
        if not isinstance(current, dict):
            current = {}
    except ValueError:
        current = {}
    merged = {**current, **want.encoder_settings()}
    if merged != current and _write(enc_path, json.dumps(merged, indent=4), report):
        report["changed"].append("streamEncoder.json")

    report["restart"] = bool(report["changed"])
    return report


def _write(path: Path, text: str, report: dict, *, bom: bool = False) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # OBS writes its ini files with a UTF-8 BOM and reads them either way;
        # mirror it so a diff against OBS's own write is empty.
        path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
        return True
    except OSError as e:
        report["warnings"].append(f"could not write {path} ({e})")
        return False
