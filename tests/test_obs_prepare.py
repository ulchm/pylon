"""Editing OBS's own config files: the half of setup the WebSocket cannot do.

The stakes here are higher than anywhere else in the suite, because this writes to
files OBS owns and a person did not ask us to touch. Two properties carry most of
these tests:

  * **additive**, never replacing. A PC that already runs another broadcaster has a
    websocket password and a dock of its own, and both must survive untouched.
  * **it refuses rather than wasting a change.** OBS rewrites these on exit, so an
    edit made while it is running is silently undone, which is worse than not
    editing at all because the operator believes it worked.
"""

from __future__ import annotations

import json

import pytest

from pylon.obs import prepare
from pylon.obs.prepare import (
    add_browser_dock,
    enable_websocket,
    prepare_obs,
    user_ini_path,
    websocket_config_path,
)

# A real user.ini, trimmed: the long base64 DockState is what must survive intact.
USER_INI = """\
[General]
Pre19Defaults=false
LastVersion=520093697

[BasicWindow]
gridMode=false
DockState=AAAA/wAAAAD9AAAAAwAAAAAAAADnAAADlPwCAAAAAvsAAAAUAHMAYwBlAG4AZQBzAEQAbwBjAGsB
SideDocks=true
DocksLocked=false

[Audio]
SampleRate=48000
"""

OTHER_DOCK = {"title": "iRacing Broadcaster Control",
              "url": "http://127.0.0.1:8782/control.html",
              "uuid": "db9b436b3eef4d84802ba6c820744088"}


@pytest.fixture
def obs(tmp_path, monkeypatch):
    """An OBS config tree of our own, with OBS 'closed'."""
    monkeypatch.setattr(prepare, "obs_is_running", lambda: False)
    (tmp_path / "plugin_config" / "obs-websocket").mkdir(parents=True)
    (tmp_path / "user.ini").write_text(USER_INI, encoding="utf-8")
    return tmp_path


def _ws(obs):
    return json.loads(websocket_config_path(obs).read_text())


def _docks(obs):
    for line in user_ini_path(obs).read_text().splitlines():
        if line.startswith("ExtraBrowserDocks="):
            return json.loads(line.split("=", 1)[1])
    return None


# --- the websocket server -------------------------------------------------------

def test_a_websocket_that_was_never_enabled_is_enabled_with_a_password(obs):
    websocket_config_path(obs).write_text(json.dumps({
        "server_enabled": False, "server_password": "", "server_port": 4455}))
    changed, msg = enable_websocket(base=obs)
    assert changed and "enabled" in msg
    doc = _ws(obs)
    assert doc["server_enabled"] is True
    assert doc["auth_required"] is True
    assert len(doc["server_password"]) >= 16, "a blank password is not a password"


def test_a_password_obs_already_generated_is_never_touched(obs):
    """Another broadcaster on this PC is authenticating with it."""
    websocket_config_path(obs).write_text(json.dumps({
        "server_enabled": False, "server_password": "the-one-obs-made", "server_port": 4455}))
    enable_websocket(base=obs)
    assert _ws(obs)["server_password"] == "the-one-obs-made"


def test_an_already_enabled_server_is_left_completely_alone(obs):
    before = json.dumps({"server_enabled": True, "server_password": "keep-me",
                         "server_port": 4455, "auth_required": True})
    websocket_config_path(obs).write_text(before)
    changed, msg = enable_websocket(base=obs)
    assert not changed and "already" in msg
    assert websocket_config_path(obs).read_text() == before, "not one byte"


def test_a_missing_file_is_written_rather_than_refused(obs):
    """OBS has never run, or never loaded the plugin. Telling somebody to go and
    start OBS first is a step, and removing steps is the entire point."""
    assert not websocket_config_path(obs).exists()
    changed, _msg = enable_websocket(base=obs)
    assert changed
    doc = _ws(obs)
    assert doc["server_enabled"] is True and doc["server_port"] == 4455
    assert doc["server_password"]


def test_a_config_we_cannot_parse_is_left_alone(obs):
    """Better a manual tick-box than a clobbered file."""
    websocket_config_path(obs).write_text("{ this is not json")
    changed, msg = enable_websocket(base=obs)
    assert not changed and "could not read" in msg
    assert websocket_config_path(obs).read_text() == "{ this is not json"


# --- the control panel dock ------------------------------------------------------

def test_the_dock_is_added_with_a_uuid_shaped_like_obs_own(obs):
    changed, msg = add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    assert changed and "added" in msg
    docks = _docks(obs)
    assert len(docks) == 1
    assert docks[0]["title"] == "Pylon"
    assert docks[0]["url"] == "http://127.0.0.1:8882/"
    assert len(docks[0]["uuid"]) == 32 and "-" not in docks[0]["uuid"]


def test_another_broadcasters_dock_survives(obs):
    """THE test. The author's own sim rig has one of these, and losing it would be
    a self-inflicted outage on the show this product was cut from."""
    p = user_ini_path(obs)
    p.write_text(USER_INI.replace("SideDocks=true",
                                  f"ExtraBrowserDocks={json.dumps([OTHER_DOCK])}\nSideDocks=true"))
    add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    docks = _docks(obs)
    assert len(docks) == 2
    assert OTHER_DOCK in docks, "the other broadcaster's dock was changed"
    assert any(d["title"] == "Pylon" for d in docks)


def test_adding_twice_does_not_add_twice(obs):
    add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    changed, msg = add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    assert not changed and "already" in msg
    assert len(_docks(obs)) == 1


def test_a_renamed_dock_is_still_our_dock(obs):
    """Matched on the URL, not the title: somebody who renamed it still has it, and
    a second one pointing at the same page is the clutter this is meant to save."""
    p = user_ini_path(obs)
    mine = {"title": "My Director", "url": "http://127.0.0.1:8882/", "uuid": "a" * 32}
    p.write_text(USER_INI.replace("SideDocks=true",
                                  f"ExtraBrowserDocks={json.dumps([mine])}\nSideDocks=true"))
    changed, _ = add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    assert not changed
    assert _docks(obs) == [mine]


def test_everything_else_in_the_file_survives_byte_for_byte(obs):
    """A long base64 DockState is somebody's whole window layout, and the file is
    full of keys OBS owns. Only the one line may change."""
    before = user_ini_path(obs).read_text().splitlines()
    add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    after = user_ini_path(obs).read_text().splitlines()
    added = [ln for ln in after if ln not in before]
    assert len(added) == 1 and added[0].startswith("ExtraBrowserDocks=")
    assert [ln for ln in before if ln not in after] == [], "a line was lost"
    assert any("DockState=AAAA" in ln for ln in after)


def test_the_original_is_backed_up_before_the_first_edit(obs):
    add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    backup = user_ini_path(obs).with_suffix(".ini.pylon-backup")
    assert backup.is_file()
    assert backup.read_text() == USER_INI


def test_a_file_with_no_basic_window_section_is_refused_not_guessed(obs):
    """The key does nothing outside [BasicWindow], and a change that silently does
    nothing is worse than being told to click six times."""
    user_ini_path(obs).write_text("[General]\nLastVersion=1\n")
    changed, msg = add_browser_dock("Pylon", "http://x/", base=obs)
    assert not changed and "by hand" in msg


def test_a_dock_list_obs_wrote_that_we_cannot_parse_is_replaced_not_merged(obs):
    """Nothing can be preserved from a line that does not parse, so the honest
    thing is to write a good one rather than drop ours on the floor."""
    user_ini_path(obs).write_text(
        USER_INI.replace("SideDocks=true", "ExtraBrowserDocks=[not json\nSideDocks=true"))
    changed, _ = add_browser_dock("Pylon", "http://127.0.0.1:8882/", base=obs)
    assert changed
    assert [d["title"] for d in _docks(obs)] == ["Pylon"]


# --- the two together ------------------------------------------------------------

def test_a_running_obs_is_refused_because_the_edit_would_be_undone(obs, monkeypatch):
    """OBS rewrites these on exit. An edit it will undo is worse than no edit,
    because the operator believes it worked."""
    monkeypatch.setattr(prepare, "obs_is_running", lambda: True)
    rep = prepare_obs(panel_url="http://127.0.0.1:8882/", base=obs)
    assert not rep.ok and rep.obs_running and not rep.changed
    assert any("Close OBS" in ln for ln in rep.lines)
    assert _docks(obs) is None, "nothing was written"


def test_no_obs_installed_says_so_and_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "obs_is_running", lambda: False)
    rep = prepare_obs(panel_url="http://x/", base=tmp_path / "not-here")
    assert not rep.ok and not rep.changed
    assert any("does not seem to be installed" in ln for ln in rep.lines)


def test_a_full_run_reports_what_it_changed_and_is_idempotent(obs):
    first = prepare_obs(panel_url="http://127.0.0.1:8882/", base=obs)
    assert first.ok and first.changed
    assert any("Start OBS now" in ln for ln in first.lines)

    second = prepare_obs(panel_url="http://127.0.0.1:8882/", base=obs)
    assert second.ok and not second.changed
    assert any("Nothing to do" in ln for ln in second.lines)
