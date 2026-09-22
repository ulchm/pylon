"""The control panel's settings API: the form that replaces editing a file.

This is the only way most people will ever change a setting, so the failures worth
guarding are the ones that lose someone's work or leak something:

  * a save must MERGE, never replace, or a panel showing one tab blanks the others
  * the stream key must not come back out over a port with no authentication
  * ...and must not be wiped by a save from the form that never saw it
  * a bad value must be reported, not silently written or silently dropped
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from pylon.config import Config, LookConfig, ObsConfig, ShowConfig, load, save
from pylon.show.dock import _ControlHandler, serve_control


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """A served panel over a config file of our own, with no studio behind it."""
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("PYLON_CONFIG", str(cfg_path))
    srv = serve_control(None, host="127.0.0.1", port=0)
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", cfg_path
    finally:
        srv.shutdown()


def _get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(base, path, payload):
    req = urllib.request.Request(f"{base}{path}", data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- reading -------------------------------------------------------------------

def test_the_form_is_served_with_the_whole_config_and_where_it_lives(panel):
    base, cfg_path = panel
    save(Config(show=ShowConfig(name="Thursday Night Racing")), cfg_path)
    code, doc = _get(base, "/api/settings")
    assert code == 200
    assert doc["settings"]["show"]["name"] == "Thursday Night Racing"
    assert set(doc["settings"]) == {"show", "look", "obs", "director", "advanced"}
    assert doc["path"] == str(cfg_path)


def test_a_secret_is_shown_as_set_without_being_sent(panel):
    """The panel is a browser page on a port with no authentication. A stream key is
    the one value here that lets a stranger broadcast as the operator."""
    base, cfg_path = panel
    save(Config(obs=ObsConfig(password="hunter2", stream_key="live_1_secret")), cfg_path)
    _code, doc = _get(base, "/api/settings")
    body = json.dumps(doc)
    assert "hunter2" not in body and "live_1_secret" not in body
    assert doc["settings"]["obs"]["stream_key"] == _ControlHandler.REDACTED
    assert doc["settings"]["obs"]["password"] == _ControlHandler.REDACTED


def test_a_secret_that_is_not_set_is_not_masked(panel):
    """An empty box has to look empty, or nobody can tell whether one is configured."""
    base, cfg_path = panel
    save(Config(), cfg_path)
    _code, doc = _get(base, "/api/settings")
    assert doc["settings"]["obs"]["stream_key"] == ""


def test_a_broken_file_is_reported_to_the_form_rather_than_failing_the_request(panel):
    base, cfg_path = panel
    cfg_path.write_text("[show\nname = broken")
    code, doc = _get(base, "/api/settings")
    assert code == 200
    assert doc["problems"] and "could not be read" in doc["problems"][0]


# --- saving --------------------------------------------------------------------

def test_a_save_merges_and_never_blanks_what_it_did_not_send(panel):
    """The panel may be an older build, or showing one tab. A save must not wipe a
    section it never displayed."""
    base, cfg_path = panel
    save(Config(show=ShowConfig(name="TNR", subtitle="Watkins Glen"),
                look=LookConfig(colour="#E11D48")), cfg_path)
    code, doc = _post(base, "/api/settings", {"show": {"round": "Round 9"}})
    assert code == 200 and doc["ok"]
    back = load(cfg_path)
    assert back.show.round == "Round 9"
    assert back.show.name == "TNR", "the field the form did not send"
    assert back.look.colour == "#E11D48", "the section the form did not send"


def test_a_masked_secret_sent_back_unchanged_keeps_the_real_one(panel):
    """Which is what lets the form edit everything else without ever holding the key."""
    base, cfg_path = panel
    save(Config(obs=ObsConfig(stream_key="live_1_secret")), cfg_path)
    _code, doc = _get(base, "/api/settings")
    doc["settings"]["obs"]["transition"] = "Cut"
    _post(base, "/api/settings", doc["settings"])
    assert load(cfg_path).obs.stream_key == "live_1_secret"
    assert load(cfg_path).obs.transition == "Cut"


def test_a_secret_can_still_be_changed_and_cleared(panel):
    """The mask must not become a cage: typing a new key has to work, and emptying
    the box has to mean emptying it."""
    base, cfg_path = panel
    save(Config(obs=ObsConfig(stream_key="old_key")), cfg_path)
    _post(base, "/api/settings", {"obs": {"stream_key": "new_key"}})
    assert load(cfg_path).obs.stream_key == "new_key"
    _post(base, "/api/settings", {"obs": {"stream_key": ""}})
    assert load(cfg_path).obs.stream_key == ""


def test_a_key_the_form_invented_is_ignored_rather_than_written(panel):
    base, cfg_path = panel
    save(Config(), cfg_path)
    code, _doc = _post(base, "/api/settings", {"show": {"nonsense": "x"},
                                               "nosuchtable": {"a": 1}})
    assert code == 200
    assert "nonsense" not in cfg_path.read_text()


def test_a_bad_value_is_reported_back_to_the_form_and_the_default_stands(panel):
    """Typing letters into the port box tells you so, rather than failing at the next
    restart with a worker that will not come up."""
    base, cfg_path = panel
    save(Config(), cfg_path)
    code, doc = _post(base, "/api/settings", {"advanced": {"bridge_port": "not a port"}})
    assert code == 200
    assert doc["problems"] and "bridge_port" in doc["problems"][0]
    assert load(cfg_path).advanced.bridge_port == 8779


def test_a_body_that_is_not_json_is_a_bad_request_not_a_traceback(panel):
    base, _cfg_path = panel
    req = urllib.request.Request(f"{base}/api/settings", data=b"{not json",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected a 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_saving_re_reads_the_ports_the_rest_of_the_process_uses(panel):
    """`SHOW` is built from the config at import. A save that did not reload it would
    leave the running studio probing the old ports with no sign of why."""
    from pylon import settings

    base, cfg_path = panel
    save(Config(), cfg_path)
    _post(base, "/api/settings", {"advanced": {"bridge_port": 9123}})
    try:
        assert settings.SHOW.bridge_port == 9123
    finally:
        # Put the module global back: it is process-wide state and the rest of the
        # suite reads it.
        cfg_path.unlink()
        settings.reload_show()
