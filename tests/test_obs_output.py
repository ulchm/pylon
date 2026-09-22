"""The stream output is provisioned, not left to OBS's "auto".

Round 1 (2026-09-20) went out with a keyframe every 4.17s because Simple output
mode leaves the interval on auto and a Custom stream service applies no rules.
These tests pin the mechanism: the ini rewrite leaves OBS's file looking like
OBS wrote it, the encoder file gets the parameters, and re-running changes
nothing.
"""

import json
from types import SimpleNamespace

from pylon.obs.output import (
    OutputSettings,
    ensure_output,
    ini_value,
    ini_with,
    profile_dir,
)

# The rig's profile as OBS 32 wrote it on 2026-09-20 (trimmed): Simple mode, x264
# as the never-used Advanced encoder, a UTF-8 BOM in front.
RIG_INI = """\
[General]
Name=Untitled

[Output]
Mode=Simple

[SimpleOutput]
VBitrate=6000
ABitrate=160
NVENCPreset2=p5
StreamEncoder=nvenc

[AdvOut]
ApplyServiceSettings=true
TrackIndex=1
Encoder=obs_x264
RecType=Standard
Track1Bitrate=160
AudioEncoder=ffmpeg_aac

[Video]
BaseCX=1920
FPSNum=60
"""


class FakeOBS:
    """obs-websocket's profile parameters, as a dict OBS holds in memory."""

    def __init__(self, params=None, profile="Untitled"):
        self.params = dict(params or {})
        self.profile = profile
        self.sets: list = []

    def get_profile_parameter(self, category, name):
        return SimpleNamespace(parameter_value=self.params.get((category, name)))

    def set_profile_parameter(self, category, name, value):
        self.params[(category, name)] = value
        self.sets.append((category, name, value))

    def get_profile_list(self):
        return SimpleNamespace(current_profile_name=self.profile)


def obs_tree(tmp_path, ini=RIG_INI, encoder=None, user_ini=True):
    base = tmp_path / "obs-studio"
    pdir = base / "basic/profiles/Untitled"
    pdir.mkdir(parents=True)
    if user_ini:
        (base / "user.ini").write_text("[Basic]\nProfile=Untitled\nProfileDir=Untitled\n",
                                       encoding="utf-8-sig")
    (pdir / "basic.ini").write_text(ini, encoding="utf-8-sig")
    (pdir / "streamEncoder.json").write_text(encoder if encoder is not None else "{}")
    return base, pdir


# --- the ini rewrite -----------------------------------------------------------

def test_ini_values_are_replaced_in_place_and_nothing_else_moves():
    out = ini_with(RIG_INI, {("Output", "Mode"): "Advanced",
                             ("AdvOut", "Encoder"): "obs_nvenc_h264_tex"})
    assert ini_value(out, "Output", "Mode") == "Advanced"
    assert ini_value(out, "AdvOut", "Encoder") == "obs_nvenc_h264_tex"
    # two lines differ, the rest is byte for byte
    diff = [(a, b) for a, b in zip(RIG_INI.splitlines(), out.splitlines(), strict=True)
            if a != b]
    assert diff == [("Mode=Simple", "Mode=Advanced"),
                    ("Encoder=obs_x264", "Encoder=obs_nvenc_h264_tex")]


def test_a_new_key_lands_in_its_section_and_a_new_section_is_appended():
    out = ini_with(RIG_INI, {("AdvOut", "Track2Bitrate"): "128", ("Stream1", "X"): "1"})
    lines = out.splitlines()
    i = lines.index("Track2Bitrate=128")
    assert lines[i - 1] == "AudioEncoder=ffmpeg_aac"     # end of [AdvOut]
    assert lines[i + 1] == "" and lines[i + 2] == "[Video]"   # the blank line survives
    assert lines[-2:] == ["[Stream1]", "X=1"]


def test_the_rewrite_is_a_fixed_point():
    want = OutputSettings().profile_settings()
    once = ini_with(RIG_INI, want)
    assert ini_with(once, want) == once


# --- ensure_output -------------------------------------------------------------

def test_first_run_switches_to_advanced_nvenc_and_writes_the_encoder(tmp_path):
    base, pdir = obs_tree(tmp_path)
    fake = FakeOBS({("Output", "Mode"): "Simple", ("AdvOut", "Encoder"): "obs_x264",
                    ("AdvOut", "TrackIndex"): "1", ("AdvOut", "AudioEncoder"): "ffmpeg_aac",
                    ("AdvOut", "Track1Bitrate"): "160"})
    rep = ensure_output(fake, OutputSettings(), base=base)

    assert rep["restart"] and not rep["warnings"]
    assert rep["changed"] == ["Output/Mode=Advanced", "AdvOut/Encoder=obs_nvenc_h264_tex",
                              "streamEncoder.json"]
    # OBS's memory, over the websocket
    assert fake.sets == [("Output", "Mode", "Advanced"),
                         ("AdvOut", "Encoder", "obs_nvenc_h264_tex")]
    # and the disk underneath it, with OBS's BOM
    raw = (pdir / "basic.ini").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert ini_value(text, "Output", "Mode") == "Advanced"
    assert ini_value(text, "SimpleOutput", "VBitrate") == "6000"      # untouched
    enc = json.loads((pdir / "streamEncoder.json").read_text())
    assert enc["keyint_sec"] == 2 and enc["bitrate"] == 8000 and enc["preset"] == "p6"
    assert enc["multipass"] == "fullres" and enc["bframe_ref_mode"] == 2


def test_second_run_is_a_no_op(tmp_path):
    base, pdir = obs_tree(tmp_path)
    fake = FakeOBS({("Output", "Mode"): "Simple", ("AdvOut", "Encoder"): "obs_x264"})
    ensure_output(fake, OutputSettings(), base=base)
    fake.sets.clear()
    before = (pdir / "basic.ini").read_bytes(), (pdir / "streamEncoder.json").read_bytes()

    rep = ensure_output(fake, OutputSettings(), base=base)
    assert rep["changed"] == [] and not rep["restart"]
    assert fake.sets == []
    assert before == ((pdir / "basic.ini").read_bytes(),
                      (pdir / "streamEncoder.json").read_bytes())


def test_a_force_killed_obs_lost_its_memory_but_the_disk_still_gets_pinned(tmp_path):
    """OBS only writes basic.ini on a clean exit. After a force-kill the websocket
    already answers Advanced (memory set last time) while the file says Simple."""
    base, pdir = obs_tree(tmp_path)
    fake = FakeOBS({("Output", "Mode"): "Advanced", ("AdvOut", "Encoder"): "obs_nvenc_h264_tex",
                    ("AdvOut", "TrackIndex"): "1", ("AdvOut", "AudioEncoder"): "ffmpeg_aac",
                    ("AdvOut", "Track1Bitrate"): "160"})
    rep = ensure_output(fake, OutputSettings(), base=base)
    assert fake.sets == []                                   # memory was right
    assert "Output/Mode=Advanced" in rep["changed"]          # the disk was not
    assert ini_value((pdir / "basic.ini").read_text(encoding="utf-8-sig"),
                     "Output", "Mode") == "Advanced"


def test_obs_not_running_still_pins_the_files(tmp_path):
    base, pdir = obs_tree(tmp_path)
    rep = ensure_output(None, OutputSettings(bitrate=6000), base=base)
    assert rep["restart"] and not rep["warnings"]
    assert json.loads((pdir / "streamEncoder.json").read_text())["bitrate"] == 6000
    assert ini_value((pdir / "basic.ini").read_text(encoding="utf-8-sig"),
                     "AdvOut", "Encoder") == "obs_nvenc_h264_tex"


def test_encoder_settings_merge_over_what_obs_already_wrote(tmp_path):
    base, pdir = obs_tree(tmp_path, encoder=json.dumps({"device": "auto", "bitrate": 6000}))
    ensure_output(None, OutputSettings(), base=base)
    enc = json.loads((pdir / "streamEncoder.json").read_text())
    assert enc["device"] == "auto" and enc["bitrate"] == 8000


def test_the_profile_dir_comes_from_user_ini_then_the_websocket(tmp_path):
    base, pdir = obs_tree(tmp_path)
    assert profile_dir(None, base) == pdir
    base2, pdir2 = obs_tree(tmp_path / "two", user_ini=False)
    assert profile_dir(None, base2) is None
    assert profile_dir(FakeOBS(), base2) == pdir2


def test_no_profile_is_a_warning_not_a_crash(tmp_path):
    rep = ensure_output(None, OutputSettings(), base=tmp_path / "nowhere")
    assert rep["changed"] == [] and any("profile directory" in w for w in rep["warnings"])


def test_the_report_describes_the_encode_for_the_console():
    assert OutputSettings().describe() == \
        "NVENC H.264 CBR 8000 kbps, keyframe 2s, p6 full-res multipass, AAC 160k"
