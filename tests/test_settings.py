"""ShowSettings: the numbers the show is wired with, and the URLs built from them."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pylon import settings
from pylon.settings import SHOW, ShowSettings


def test_the_urls_are_built_from_the_ports_so_they_cannot_disagree():
    s = ShowSettings(bridge_port=4, overlay_ws_port=2, overlay_http_port=1, control_port=6)
    assert s.overlay_page_url() == "http://localhost:1/broadcast-overlay.html?ws=ws://localhost:2&live"
    assert s.cards_url() == "http://localhost:1/cards.html"
    assert s.bridge_url() == "ws://127.0.0.1:4"
    assert s.bridge_url(host="simpc") == "ws://simpc:4"
    assert s.bridge_url(port=9) == "ws://127.0.0.1:9"
    assert s.control_url() == "http://127.0.0.1:6/"


def test_the_pages_can_be_addressed_by_lan_name_for_a_preview_elsewhere():
    assert SHOW.overlay_page_url(host="simpc").startswith("http://simpc:")
    assert "ws://simpc:" in SHOW.overlay_page_url(host="simpc")


def test_every_listening_port_is_distinct():
    ports = [SHOW.bridge_port, SHOW.overlay_ws_port, SHOW.overlay_http_port,
             SHOW.control_port, SHOW.obs_port]
    assert len(set(ports)) == len(ports)


def test_the_overlays_dir_holds_every_page_the_show_serves():
    """The tower, the cards and the control panel all come off one directory,
    resolved in one place. It used to be two copies of a parents[2] trick."""
    for page in ("broadcast-overlay.html", "cards.html", "control.html"):
        assert (SHOW.overlays_dir / page).is_file(), page


def test_the_ports_come_from_the_config_so_one_change_moves_every_worker():
    """The whole point of building SHOW from the config: an operator changing a port
    under [advanced] changes the port the worker binds AND the one the studio probes,
    because both read this."""
    from pylon.config import AdvancedConfig, Config

    s = ShowSettings.from_config(Config(advanced=AdvancedConfig(bridge_port=9100,
                                                                control_port=9101)))
    assert s.bridge_port == 9100
    assert s.bridge_url() == "ws://127.0.0.1:9100"
    assert s.control_url() == "http://127.0.0.1:9101/"


def test_a_config_that_cannot_be_read_leaves_the_defaults_standing(monkeypatch):
    """A broken settings file must never stop the package importing: the show comes
    up on the defaults and `pylon doctor` is where the problem gets reported."""
    monkeypatch.setattr(settings, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert settings._show_from_config() == ShowSettings()


def test_a_handoff_file_moves_with_its_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("PYLON_SHOT_FILE", str(tmp_path / "s.json"))
    assert settings._handoff_file("PYLON_SHOT_FILE", "x.json") == tmp_path / "s.json"
    monkeypatch.delenv("PYLON_SHOT_FILE")
    assert settings._handoff_file("PYLON_SHOT_FILE", "x.json").name == "x.json"


def test_the_settings_are_frozen():
    with pytest.raises(FrozenInstanceError):
        SHOW.bridge_port = 1  # type: ignore[misc]
