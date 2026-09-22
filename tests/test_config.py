"""The settings file: the one surface an operator actually edits.

Everything here guards the same promise. A person who has never opened a terminal
will edit this file, or the form over it, and nothing they can type may stop a
broadcast that is about to start. So: a bad value is reported and ignored, a
broken file leaves the defaults standing, and a save never loses what it did not
understand.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from pylon import config as cfgmod
from pylon.config import (
    COMMENTS,
    Config,
    DirectorConfigFile,
    LookConfig,
    ObsConfig,
    ShowConfig,
    as_dict,
    ensure_config,
    from_dict,
    load,
    save,
    to_toml,
)

# --- reading ------------------------------------------------------------------

def test_no_file_at_all_is_a_working_show_not_an_error():
    """The first-run case. Defaults have to BE a broadcast, because the first thing
    anyone does is start it before reading anything."""
    cfg = load(tmp_missing := cfgmod.Path("/definitely/not/here/config.toml"))
    assert cfg == Config()
    assert cfg.problems == []
    assert not tmp_missing.exists()


def test_values_from_the_file_win_over_the_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[show]\nname = "Thursday Night Racing"\n\n[look]\ncolour = "#ff0000"\n')
    cfg = load(p)
    assert cfg.show.name == "Thursday Night Racing"
    assert cfg.look.colour == "#ff0000"
    assert cfg.look.tower is True, "a section that named one key keeps the rest"


def test_a_file_that_does_not_parse_reports_it_and_runs_on_the_defaults(tmp_path):
    """The case that matters most: it is five minutes to the green flag and someone
    has left a quote off. The show must still go on air, and something must say why
    it looks wrong."""
    p = tmp_path / "config.toml"
    p.write_text('[show\nname = "broken')
    cfg = load(p)
    assert cfg == Config()
    assert len(cfg.problems) == 1
    assert "could not be read" in cfg.problems[0]


def test_a_value_of_the_wrong_type_is_reported_and_the_default_stands(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[advanced]\nbridge_port = "not a number"\n\n[look]\ntower = "maybe"\n')
    cfg = load(p)
    assert cfg.advanced.bridge_port == 8779
    assert cfg.look.tower is True
    assert len(cfg.problems) == 2
    assert any("bridge_port" in p for p in cfg.problems)


@pytest.mark.parametrize("written,want", [
    ("true", True), ("yes", True), ("on", True), ('"1"', True),
    ("false", False), ('"no"', False), ('"off"', False),
])
def test_a_boolean_written_the_way_a_person_writes_one(tmp_path, written, want):
    """TOML has real booleans, but someone editing a config file types "yes"."""
    p = tmp_path / "config.toml"
    p.write_text(f"[director]\nreplays = {written}\n")
    assert load(p).director.replays is want


def test_a_port_quoted_as_text_is_still_a_port(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[advanced]\nbridge_port = "9100"\n')
    cfg = load(p)
    assert cfg.advanced.bridge_port == 9100
    assert cfg.problems == []


def test_favourites_accept_a_list_or_the_line_a_person_types(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[director]\nfavourites = "64, 17 , "\n')
    assert load(p).director.favourites == ["64", "17"]
    p.write_text('[director]\nfavourites = ["64", 17]\n')
    assert load(p).director.favourites == ["64", "17"]


def test_a_section_that_is_not_a_section_is_reported_not_fatal(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('show = "Thursday Night Racing"\n')
    cfg = load(p)
    assert cfg.show.name == ""
    assert any("should be a section" in p for p in cfg.problems)


# --- writing ------------------------------------------------------------------

def test_every_field_is_written_with_the_comment_that_explains_it():
    """A config file's comments ARE its documentation for the person this is for, so
    a field added without one fails here rather than shipping unexplained."""
    missing = []
    for section in fields(Config):
        if section.name in ("problems", "extra"):
            continue
        assert section.name in COMMENTS, section.name
        table = getattr(Config(), section.name)
        for f in fields(table):
            if f"{section.name}.{f.name}" not in COMMENTS:
                missing.append(f"{section.name}.{f.name}")
    assert not missing, f"no comment for: {missing}"


def test_what_is_written_reads_back_as_the_same_settings(tmp_path):
    cfg = Config(show=ShowConfig(name="Thursday Night Racing", round="Round 4"),
                 look=LookConfig(colour="#E11D48", tower=False),
                 director=DirectorConfigFile(favourites=["64", "17"], min_shot=8.5),
                 obs=ObsConfig(password='a "quoted" one', scenes=False))
    p = save(cfg, tmp_path / "config.toml")
    back = load(p)
    assert back.problems == []
    assert back.show == cfg.show
    assert back.look == cfg.look
    assert back.director == cfg.director
    assert back.obs == cfg.obs


def test_a_key_this_version_does_not_know_survives_a_save(tmp_path):
    """An older build must not silently delete a newer build's settings, and a note
    someone added by hand is theirs, not ours to throw away."""
    p = tmp_path / "config.toml"
    p.write_text('[show]\nname = "TNR"\nmotto = "keep it clean"\n\n'
                 '[future]\nsomething = 1\n')
    cfg = load(p)
    save(cfg, p)
    text = p.read_text()
    assert "motto" in text and "keep it clean" in text
    assert load(p).extra["show"]["motto"] == "keep it clean"


def test_the_file_is_written_atomically(tmp_path, monkeypatch):
    """The panel writes this while the show is running and the workers read it. A
    half-written file read by a restarting worker is default paint mid-race."""
    seen = []
    real_replace = cfgmod.os.replace
    monkeypatch.setattr(cfgmod.os, "replace",
                        lambda a, b: (seen.append((a, b)), real_replace(a, b))[1])
    p = save(Config(), tmp_path / "config.toml")
    assert seen and str(seen[0][1]) == str(p)


def test_ensure_config_writes_one_on_the_first_run_and_says_it_did(tmp_path):
    p = tmp_path / "config.toml"
    _cfg, path, created = ensure_config(p)
    assert created is True and path == p and p.is_file()
    assert "# Pylon" in p.read_text()
    # ...and only the first time. `created` is what a welcome message keys off.
    _, _, again = ensure_config(p)
    assert again is False


def test_a_place_that_cannot_be_written_is_a_problem_not_a_crash(tmp_path):
    """A read-only profile, a synced folder mid-conflict: the show still starts.

    A FILE stands in for the unwritable directory here, because the failure a
    read-only profile produces is the same OSError from the same mkdir.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    cfg, _path, created = ensure_config(blocker / "config.toml")
    assert created is False
    assert any("could not write" in p for p in cfg.problems)
    assert cfg.show == Config().show, "and the defaults still stand"


# --- what the pages are allowed to see ----------------------------------------

def test_the_page_view_carries_no_secret():
    """The overlay pages are rendered by a browser and served off a port with no
    authentication, so nothing that lets a stranger broadcast may go out on it."""
    cfg = Config(obs=ObsConfig(password="hunter2", stream_key="live_1_secret"))
    body = str(cfg.page_view())
    assert "hunter2" not in body and "live_1_secret" not in body


def test_the_tag_falls_back_to_the_name_so_a_page_always_has_something_to_draw():
    assert Config(show=ShowConfig(name="Thursday Night Racing")).page_view()["tag"] \
        == "Thursday Night Racing"
    assert Config(show=ShowConfig(name="Thursday Night Racing", tag="TNR")) \
        .page_view()["tag"] == "TNR"


def test_as_dict_is_the_whole_form_and_nothing_internal():
    doc = as_dict(Config())
    assert set(doc) == {"show", "look", "obs", "director", "advanced"}
    assert "problems" not in doc and "extra" not in doc
    # round trip: the panel sends this shape straight back
    assert from_dict(doc).show == Config().show


def test_the_written_file_is_valid_toml_even_with_awkward_text():
    r"""Someone will put a backslash in a Windows path and a quote in a show name."""
    cfg = Config(show=ShowConfig(name='The "Real" Show'),
                 look=LookConfig(logo=r"C:\art\logo.svg"))
    text = to_toml(cfg)
    import tomllib
    doc = tomllib.loads(text)
    assert doc["show"]["name"] == 'The "Real" Show'
    assert doc["look"]["logo"] == r"C:\art\logo.svg"
