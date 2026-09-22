"""The overlay's JavaScript, under a real headless Chrome.

overlays/overlay.js is 950 lines that had no tests: the pop-ins, the fastest-lap toast,
the tower window and the replay wipe were verified by hand over the DevTools protocol,
and that procedure lived in a memory note. This is that procedure as a test.

The harness serves overlays/ with the production handler, loads
broadcast-overlay.html?harness (which starts neither the WebSocket nor the built-in
mock), stubs performance.now so nothing depends on wall time, and hands
window.overlay.render() models built by the real TowerModel from the synthetic source.
Assertions are on the DOM the viewer sees, never on script internals.

Why not --virtual-time-budget screenshots: the budget does not advance performance.now,
so nothing time-driven ever happens, and appending a <script>render(model)</script> to
the page used to fail silently because render was closed inside the IIFE while the mock
painted a plausible overlay in its place. Both cost an afternoon once.

Skipped when no Chrome is on the box (CI has none); PYLON_CHROME names a binary.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import websockets

from pylon.config import Config, LookConfig, ShowConfig
from pylon.director.model import Shot, ShotKind
from pylon.overlay.model import TowerModel
from pylon.overlay.transport import _OverlayHTTP
from pylon.settings import SHOW
from pylon.telemetry import SyntheticSource
from pylon.world import WorldModel

PAGE = "broadcast-overlay.html"


# --- static: the page and its script, no browser needed ---------------------------

def test_the_page_loads_its_script_from_overlay_js():
    html = (SHOW.overlays_dir / PAGE).read_text(encoding="utf-8")
    assert '<script src="overlay.js"></script>' in html
    assert "(() => {" not in html, "the IIFE must live in overlay.js, not inline"
    assert (SHOW.overlays_dir / "overlay.js").is_file()


def test_overlay_js_exposes_render_and_a_harness_mode_and_is_served_as_utf8():
    js = (SHOW.overlays_dir / "overlay.js").read_text(encoding="utf-8")
    assert "window.overlay = Object.freeze({ render })" in js
    assert 'params.has("harness")' in js
    h = _OverlayHTTP.__new__(_OverlayHTTP)  # guess_type touches no instance state
    assert "charset=utf-8" in h.guess_type("overlay.js")


# --- the harness --------------------------------------------------------------------

def _chrome() -> str | None:
    for name in (os.environ.get("PYLON_CHROME"), "google-chrome-stable", "google-chrome",
                 "chromium", "chromium-browser"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


class Page:
    """One DevTools connection to the page: evaluate JavaScript, collect exceptions."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.loop = asyncio.new_event_loop()
        self.ws = None
        self.exceptions: list = []
        self._id = 0

    def open(self) -> None:
        self.ws = self.loop.run_until_complete(websockets.connect(self.ws_url, max_size=None))
        self._call("Runtime.enable")

    def close(self) -> None:
        if self.ws is not None:
            self.loop.run_until_complete(self.ws.close())
        self.loop.close()

    def _call(self, method: str, **params) -> dict:
        async def go():
            self._id += 1
            await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
            while True:
                msg = json.loads(await self.ws.recv())
                if msg.get("method") == "Runtime.exceptionThrown":
                    self.exceptions.append(msg["params"]["exceptionDetails"])
                if msg.get("id") == self._id:
                    return msg
        return self.loop.run_until_complete(asyncio.wait_for(go(), 15.0))

    def eval(self, expression: str):
        r = self._call("Runtime.evaluate", expression=expression, returnByValue=True,
                       awaitPromise=True)
        if "exceptionDetails" in r.get("result", {}):
            raise AssertionError(f"page threw: {r['result']['exceptionDetails']}")
        return r["result"]["result"].get("value")

    def wait_for(self, expression: str, timeout: float = 5.0):
        """Poll until `expression` is truthy; the value it settled on."""
        deadline = time.time() + timeout
        while True:
            v = self.eval(expression)
            if v:
                return v
            if time.time() > deadline:
                raise AssertionError(f"never true: {expression}")
            time.sleep(0.05)

    def render(self, model: dict) -> None:
        self.eval(f"window.overlay.render({json.dumps(model)}); true")

    def tick(self, ms: float) -> None:
        self.eval(f"window.__now += {ms}; true")


#: A configured show, the way an operator's would be. The pages read this over
#: /show.json, so a harness that did not serve it would be testing a page that
#: cannot exist.
SHOW_DOC = Config(show=ShowConfig(name="Thursday Night Racing", tag="TNR",
                                  round="Round 7", subtitle="Circuit of the Americas"),
                  look=LookConfig(colour="#E11D48")).page_view()


@pytest.fixture(scope="module")
def http_url():
    # The production handler, including its one dynamic route.
    _OverlayHTTP.show = SHOW_DOC
    handler = functools.partial(_OverlayHTTP, directory=str(SHOW.overlays_dir))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        _OverlayHTTP.show = {}


def _launch(url: str, match: str):
    """Headless Chrome on one page. Yields a Page; caller closes via the generator."""
    exe = _chrome()
    if exe is None:
        pytest.skip("no Chrome on this box (set PYLON_CHROME to name one)")
    profile = tempfile.mkdtemp(prefix="pylon-chrome-")
    args = [exe, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--mute-audio",
            "--remote-debugging-port=0", f"--user-data-dir={profile}",
            "--window-size=1920,1080", "--autoplay-policy=no-user-gesture-required", url]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pg = None
    try:
        port_file = Path(profile) / "DevToolsActivePort"
        deadline = time.time() + 30
        while not port_file.exists() or not port_file.read_text().strip():
            if proc.poll() is not None or time.time() > deadline:
                pytest.fail("Chrome did not open a DevTools port")
            time.sleep(0.1)
        port = int(port_file.read_text().splitlines()[0])
        target = None
        while target is None:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                targets = json.load(r)
            target = next((t for t in targets
                           if t.get("type") == "page" and match in t.get("url", "")), None)
            if target is None:
                if time.time() > deadline:
                    pytest.fail(f"page target never appeared: {targets}")
                time.sleep(0.1)
        pg = Page(target["webSocketDebuggerUrl"])
        pg.open()
        # Wait for the page we ASKED for, not for readyState: Chrome's first target
        # exists before it navigates, and an empty document reports 'complete', so a
        # readyState wait sails straight through and every getElementById is null.
        pg.wait_for(f"location.href.indexOf({json.dumps(match)}) >= 0", timeout=15)
        pg.wait_for("document.readyState === 'complete'", timeout=15)
        yield pg
    finally:
        if pg is not None:
            pg.close()
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture(scope="module")
def cards(http_url):
    """The holding card, which is a separate page with no WebSocket at all."""
    yield from _launch(f"{http_url}/cards.html?card=soon&track=cota", "cards.html")


@pytest.fixture(scope="module")
def page(http_url):
    exe = _chrome()
    if exe is None:
        pytest.skip("no Chrome on this box (set PYLON_CHROME to name one)")
    profile = tempfile.mkdtemp(prefix="pylon-chrome-")
    url = f"{http_url}/{PAGE}?harness"
    args = [exe, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--mute-audio",
            "--remote-debugging-port=0", f"--user-data-dir={profile}",
            "--window-size=1920,1080", "--autoplay-policy=no-user-gesture-required", url]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pg = None
    try:
        port_file = Path(profile) / "DevToolsActivePort"
        deadline = time.time() + 30
        while not port_file.exists() or not port_file.read_text().strip():
            if proc.poll() is not None or time.time() > deadline:
                pytest.fail("Chrome did not open a DevTools port")
            time.sleep(0.1)
        port = int(port_file.read_text().splitlines()[0])
        target = None
        while target is None:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                targets = json.load(r)
            target = next((t for t in targets if t.get("type") == "page" and PAGE in t.get("url", "")),
                          None)
            if target is None:
                if time.time() > deadline:
                    pytest.fail(f"page target never appeared: {targets}")
                time.sleep(0.1)
        pg = Page(target["webSocketDebuggerUrl"])
        pg.open()
        pg.wait_for("document.readyState === 'complete' && !!window.overlay", timeout=15)
        # Stub the clock: performance.now is assignable in Chrome, so simulated time is
        # window.__now, advanced by the test. No production hook needed for this part.
        pg.eval("window.__now = 0; performance.now = () => window.__now; true")
        yield pg
    finally:
        if pg is not None:
            pg.close()
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


# --- models: the real builder over the synthetic source ---------------------------

def _race(num_cars: int = 12, seed: int = 3, seconds: float = 30.0):
    src = SyntheticSource(num_cars=num_cars, duration_s=seconds, hz=10.0, seed=seed)
    info = src.session_info()
    wm = WorldModel(info)
    snap = None
    for fr in src.frames():
        snap = wm.update(fr)
    return TowerModel(info), snap


def _leader_shot(snap) -> Shot:
    idx = snap.order[0]
    return Shot(ShotKind.LEADER, f"leader:{idx}", idx, f"#{snap.cars[idx].number}")


def _battle_shot(snap, rank: int = 1) -> Shot:
    ahead, behind = snap.order[rank - 1], snap.order[rank]
    return Shot(ShotKind.BATTLE, f"battle:{ahead}:{behind}", behind,
                f"P{rank} vs P{rank + 1}", pair=(ahead, behind))


VISIBLE_ROWS = "Array.from(document.querySelectorAll('.row')).filter(r => r.style.display !== 'none')"


# --- what the viewer sees -----------------------------------------------------------

def test_the_tower_has_one_row_per_car_and_the_leader_wears_the_leader_row(page):
    tower, snap = _race()
    model = tower.build(snap, shot=_leader_shot(snap))
    page.render(model)
    assert page.eval(f"{VISIBLE_ROWS}.length") == len(model["cars"])
    leader = model["cars"][0]
    assert page.eval("document.querySelector('.row.leader').textContent").find(f"#{leader['num']}") >= 0
    assert page.eval("document.querySelector('.row.leader .gap').textContent") == "LEADER"
    assert page.eval("document.querySelector('.row.oncam') !== null")


def test_a_car_that_leaves_the_field_loses_its_row_without_a_reflow(page):
    tower, snap = _race()
    full = tower.build(snap, shot=_leader_shot(snap))
    page.render(full)
    fewer = dict(full, cars=full["cars"][:-2])
    page.render(fewer)
    assert page.eval(f"{VISIBLE_ROWS}.length") == len(full["cars"]) - 2
    assert page.eval("document.querySelectorAll('.row').length") == len(full["cars"])  # kept, hidden


def test_a_leader_shot_is_one_pop_in_with_the_drivers_name(page):
    tower, snap = _race()
    model = tower.build(snap, shot=_leader_shot(snap))
    page.render(model)
    page.wait_for("document.querySelectorAll('#popins .popin.show').length === 1")
    card = model["focus"]["cars"][0]
    text = page.eval("document.querySelector('#popins .popin.show').textContent")
    assert card["last"].upper() in text.upper() or card["full"].upper() in text.upper()
    assert page.eval("document.querySelector('#popins .popin-gap') === null")   # no pair, no chip


def test_a_battle_is_two_pop_ins_with_the_gap_between_them(page):
    tower, snap = _race()
    model = tower.build(snap, shot=_battle_shot(snap))
    model["focus"]["gap"] = 0.8            # the chip is the claim these two are racing
    page.render(model)
    page.wait_for("document.querySelectorAll('#popins .popin.show').length === 2")
    ids = [c["id"] for c in model["focus"]["cars"]]
    assert len(ids) == 2
    chip = page.wait_for("(document.querySelector('#popins .popin-gap.show') || {}).textContent")
    assert chip.startswith("+0.8")
    # back to a single car: the chip goes, one card stays
    page.render(tower.build(snap, shot=_leader_shot(snap)))
    page.wait_for("document.querySelectorAll('#popins .popin.show').length === 1")
    assert page.eval("document.querySelector('#popins .popin-gap') === null")


def test_the_fastest_lap_toast_fires_when_the_time_changes_hands_not_on_first_sight(page):
    tower, snap = _race()
    model = tower.build(snap, shot=_leader_shot(snap))
    cars = model["cars"]
    for c in cars:
        c["fastest"] = False
    cars[3]["fastest"] = True
    model["session"]["fastestLap"] = 95.123
    page.eval("document.getElementById('toast').classList.remove('show'); true")
    page.render(model)
    assert page.eval("document.getElementById('toast').classList.contains('show')") is False, \
        "the first holder seen is also what a mid-session reconnect looks like"
    cars[3]["fastest"], cars[5]["fastest"] = False, True
    model["session"]["fastestLap"] = 94.9
    page.render(model)
    assert page.eval("document.getElementById('toast').classList.contains('show')") is True
    assert page.eval("document.getElementById('toastCar').textContent") == f"#{cars[5]['num']} {cars[5]['tla']}"
    assert page.eval("document.getElementById('toastTime').textContent") == "1:34.900"
    # the holder improving their OWN time is not a change of hands
    page.eval("document.getElementById('toast').classList.remove('show'); true")
    model["session"]["fastestLap"] = 94.5
    page.render(model)
    assert page.eval("document.getElementById('toast').classList.contains('show')") is False


def test_an_instant_replay_letterboxes_the_stage_and_captions_the_moment(page):
    tower, snap = _race()
    model = tower.build(snap, shot=_leader_shot(snap))
    model["instantReplay"] = {"active": True, "phase": "rolling", "kind": "contact",
                              "car_number": "5", "driver": "Ada Lovelace", "position": 3,
                              "other_number": "12", "other_driver": "Alan Turing",
                              "missed_live": True}
    page.render(model)
    page.wait_for("document.getElementById('stage').classList.contains('is-replay')", timeout=4.0)
    assert page.eval("document.getElementById('rpyWhat').textContent") == "Contact"
    who = page.eval("document.getElementById('rpyWho').textContent")
    assert "#5 Ada Lovelace" in who and "#12 Alan Turing" in who and "we missed it live" in who
    # The way out waits RPY_OFF_MS for the banner to be gone: a single frame without
    # it is a read that lost a race with the director's per-frame rewrite (Round 1,
    # 2026-09-20), not the end of the replay, and must not wipe.
    model["instantReplay"] = None
    page.render(model)
    page.tick(100)
    assert page.eval("document.getElementById('stage').classList.contains('is-replay')") is True
    model["instantReplay"] = {"active": True, "phase": "rolling", "kind": "contact",
                              "car_number": "5", "driver": "Ada Lovelace", "position": 3}
    page.render(model)
    page.tick(1000)
    page.render(model)
    assert page.eval("document.getElementById('stage').classList.contains('is-replay')") is True
    # gone for good: the next render past the debounce flips it
    model["instantReplay"] = None
    page.render(model)
    page.tick(300)
    page.render(model)
    page.wait_for("!document.getElementById('stage').classList.contains('is-replay')", timeout=4.0)


def test_the_session_line_reads_the_clock_and_the_last_lap_inverts_it(page):
    tower, snap = _race()
    model = tower.build(snap, shot=_leader_shot(snap))
    model["session"].update(timeRemaining=754.0, lastLap=False)
    page.render(model)
    assert page.eval("document.getElementById('lap').textContent") == "12:34"
    assert page.eval("document.getElementById('lapLabel').textContent") == "to go"
    model["session"]["lastLap"] = True
    page.render(model)
    assert page.eval("document.getElementById('lap').textContent") == "LAST LAP"
    assert page.eval("document.querySelector('.cap').classList.contains('is-last-lap')") is True


def test_the_script_threw_nothing_the_whole_way_through(page):
    page.eval("1")   # drain any exception events still queued on the socket
    assert page.exceptions == [], page.exceptions


def test_a_towed_car_keeps_its_row_quiet_with_tow_in_the_outrigger(page):
    """Out of the world in a race (CarState.towed): the row stays where the order has it,
    dimmed, TOW where the lap time was, and no interval, because a flatbed has none."""
    tower, snap = _race()
    model = tower.build(snap, shot=_leader_shot(snap))
    victim = model["cars"][2]
    victim["tow"] = True
    page.render(model)
    row = ("Array.from(document.querySelectorAll('.row'))"
           f".find(r => r.querySelector('.num').textContent === '#{victim['num']}')")
    assert page.eval(f"{row}.classList.contains('tow')") is True
    assert page.eval(f"{row}.querySelector('.outrig').textContent") == "TOW"
    assert page.eval(f"{row}.querySelector('.outrig').classList.contains('is-tow')") is True
    assert page.eval(f"{row}.querySelector('.gap').textContent") == ""
    victim["tow"] = False
    page.render(model)
    assert page.eval(f"{row}.classList.contains('tow')") is False
    assert page.eval(f"{row}.querySelector('.outrig').textContent") != "TOW"


# --- the show's own identity on the pages, in a real browser ----------------------
#
# All of these describe the same facts (what this show is called, which round it is,
# what colour it wears) reaching two pages by two different routes, because one of
# them is being pushed a model fifteen times a second and the other is a static
# browser source that OBS points at a URL and forgets.


def test_the_cap_carries_the_round_from_the_settings(page):
    """The round chip comes from /show.json, not from the per-frame model.

    It is a constant for the whole session, so it is fetched once rather than
    repeated in every push. Which makes it the one piece of the cap that is NOT a
    function of the model, and therefore worth its own test.
    """
    tower, snap = _race()
    page.render(tower.build(snap, shot=_leader_shot(snap)))
    # The fetch resolves on its own; the cap picks the round up on the next render,
    # which the harness drives rather than a timer.
    page.wait_for("document.querySelector('.badge--round') !== null", timeout=10)
    assert page.eval("document.querySelector('.badge--round').textContent") == "Round 7"


def test_the_brand_colour_from_the_settings_reaches_the_page(page):
    """One colour in the settings paints the whole overlay: the page writes --brand
    and mixes the other two from it, so an operator never edits CSS."""
    page.wait_for("document.documentElement.style.getPropertyValue('--brand') !== ''",
                  timeout=10)
    assert page.eval(
        "document.documentElement.style.getPropertyValue('--brand').trim()") == "#E11D48"
    assert "E11D48" in page.eval(
        "document.documentElement.style.getPropertyValue('--brand-lift')")


def test_the_holding_card_names_the_show_and_draws_the_named_circuit(cards):
    cards.wait_for("document.querySelector('.card__circuit') !== null", timeout=10)
    assert cards.eval("document.getElementById('eyebrow').textContent") == "ROUND 7"
    assert cards.eval("document.getElementById('sub').textContent") \
        == "CIRCUIT OF THE AMERICAS"
    assert cards.eval("document.querySelector('.card__circuit').src").endswith("tracks/cota.svg")
    # With no logo configured, the show's NAME carries the card. That is the common
    # case, and a card with nothing at the top would look broken rather than clean.
    assert cards.eval("document.getElementById('name').textContent") == "Thursday Night Racing"


def test_the_openstreetmap_credit_is_drawn_with_the_circuit_and_never_without_it(cards):
    """Not optional. The geometry is ODbL data and every surface showing it owes
    the credit, so the code that adds the drawing adds the credit."""
    cards.wait_for("document.querySelector('.card__credit') !== null", timeout=10)
    credit = cards.eval("document.querySelector('.card__credit').textContent")
    assert "OpenStreetMap contributors" in credit
    assert cards.eval(
        "document.querySelectorAll('.card__circuit').length"
        " === document.querySelectorAll('.card__credit').length")


def test_a_card_on_an_unconfigured_machine_is_still_a_complete_card(http_url):
    """The first-run case, and the one that must not regress: nothing configured at
    all. No name, no round, no circuit, and no credit under a circuit that is not
    there, but still a deliberate-looking holding card."""
    _OverlayHTTP.show = {}
    try:
        gen = _launch(f"{http_url}/cards.html?card=soon", "cards.html")
        pg = next(gen)
        try:
            time.sleep(0.6)     # long enough for a fetch that WOULD have landed
            assert pg.eval("document.querySelector('.card__circuit') === null")
            assert pg.eval("document.querySelector('.card__credit') === null")
            assert pg.eval("document.getElementById('eyebrow').textContent") == ""
            assert pg.eval("document.getElementById('name').textContent") == ""
            assert pg.eval("document.getElementById('sub').textContent") \
                == "Coverage begins shortly"
            assert pg.eval("document.getElementById('title').textContent") == "Starting Soon"
        finally:
            gen.close()
    finally:
        _OverlayHTTP.show = SHOW_DOC


def test_an_explicit_parameter_still_beats_the_settings(http_url):
    """The card's existing contract: ?eyebrow= and ?sub= win, and a present-but-
    empty one means "hide this line" rather than "use the settings'"."""
    gen = _launch(f"{http_url}/cards.html?card=soon&track=cota&eyebrow=FINAL%20ROUND&sub=",
                  "cards.html")
    pg = next(gen)
    try:
        pg.wait_for("document.querySelector('.card__circuit') !== null", timeout=10)
        assert pg.eval("document.getElementById('eyebrow').textContent") == "FINAL ROUND"
        assert pg.eval("document.getElementById('sub').textContent") == ""
    finally:
        gen.close()


def test_no_track_named_means_no_circuit_and_no_credit(http_url):
    gen = _launch(f"{http_url}/cards.html?card=soon", "cards.html")
    pg = next(gen)
    try:
        pg.wait_for("document.getElementById('eyebrow').textContent !== ''", timeout=10)
        time.sleep(0.4)
        assert pg.eval("document.getElementById('eyebrow').textContent") == "ROUND 7"
        assert pg.eval("document.querySelector('.card__circuit') === null")
        assert pg.eval("document.querySelector('.card__credit') === null"), \
            "no circuit means no credit"
    finally:
        gen.close()
