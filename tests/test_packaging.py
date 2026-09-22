"""What the frozen Windows build depends on, checked from Linux.

None of this can build a real executable here, and that is not what it is for. It
guards the three things that differ between running from a checkout and running from
an installer, each of which fails silently in a way nothing else in the suite would
catch:

  * the studio spawns workers by RE-RUNNING ITSELF, and the command line for that is
    different when the interpreter is the application
  * the overlay pages have to be found next to the executable, or the timing tower
    serves 404s and it looks like OBS failing to load a browser source
  * a double-clicked shortcut has to start the broadcast, not print a usage message
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pylon import settings
from pylon.show.studio import default_workers

# --- how the studio re-runs itself ---------------------------------------------

def test_from_a_checkout_a_worker_runs_the_module():
    for w in default_workers(python="PY"):
        assert list(w.argv[:3]) == ["PY", "-m", "pylon"], w.argv


def test_frozen_a_worker_runs_the_application_itself(monkeypatch):
    """`Pylon.exe bridge --port 8779`, not `Pylon.exe -m pylon bridge ...`.

    A frozen build has no module to run: `-m pylon` would be passed straight to the
    CLI as a subcommand, every worker would reject its own argv, and the studio would
    report all three as "exits during startup" with the reason in a log nobody opens.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    ws = default_workers(python=r"C:\Program Files\Pylon\Pylon.exe")
    for w in ws:
        assert w.argv[0] == r"C:\Program Files\Pylon\Pylon.exe"
        assert "-m" not in w.argv and "pylon" not in w.argv[1:2], w.argv
    assert [w.argv[1] for w in ws] == ["bridge", "overlay", "live"]


def test_frozen_worker_command_lines_still_parse_under_the_cli(monkeypatch):
    """The same seam tests/test_cli.py guards for a checkout: a worker has to accept
    the command line the studio builds for it."""
    from pylon.cli import build_parser

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    parser = build_parser()
    for w in default_workers(python="Pylon.exe"):
        parser.parse_args(list(w.argv[1:]))       # SystemExit here is the failure


# --- where the pages live -------------------------------------------------------

def test_the_overlays_are_found_next_to_a_frozen_executable(monkeypatch, tmp_path):
    """PyInstaller unpacks data beside the executable (onedir) or into a temporary
    tree it points _MEIPASS at (onefile). Either way they are not under src/."""
    bundle = tmp_path / "Pylon"
    (bundle / "overlays").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    # Pretend the package does not carry its own copy, which is the frozen case.
    monkeypatch.setattr(settings, "PACKAGE_DIR", tmp_path / "nowhere")
    assert settings._overlays_dir() == bundle / "overlays"


def test_an_ordinary_checkout_is_unaffected():
    assert settings.bundle_dir() is None
    assert (settings._overlays_dir() / "broadcast-overlay.html").is_file()


def test_the_pages_the_installer_must_carry_are_all_there():
    """The spec ships overlays/ wholesale; this is the list that has to survive it.
    A page missing from the bundle is a browser source that renders nothing."""
    d = settings.SHOW.overlays_dir
    for page in ("broadcast-overlay.html", "overlay.js", "cards.html", "control.html"):
        assert (d / page).is_file(), page
    assert (d / "brand" / "flags.woff2").is_file(), "flags fall back to letters without it"


# --- the launcher ---------------------------------------------------------------

def _launcher():
    """Load packaging/launcher.py, which is outside the package on purpose."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "packaging" / "launcher.py"
    spec = importlib.util.spec_from_file_location("pylon_launcher", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_double_clicking_starts_the_show(monkeypatch):
    """Bare `Pylon.exe` is someone who just installed this from their desktop. A usage
    message is the wrong answer to that."""
    seen = []
    mod = _launcher()
    monkeypatch.setattr(sys, "argv", ["Pylon.exe"])
    monkeypatch.setitem(sys.modules, "pylon.cli",
                        type(sys)("pylon.cli"))
    sys.modules["pylon.cli"].main = lambda argv: (seen.append(argv), 0)[1]
    assert mod.main() == 0
    assert seen == [["studio"]]


def test_arguments_are_passed_through_untouched(monkeypatch):
    """Which is what keeps `Pylon.exe doctor` working AND lets the studio spawn its
    own workers out of the same executable."""
    seen = []
    mod = _launcher()
    monkeypatch.setattr(sys, "argv", ["Pylon.exe", "bridge", "--port", "8779"])
    monkeypatch.setitem(sys.modules, "pylon.cli", type(sys)("pylon.cli"))
    sys.modules["pylon.cli"].main = lambda argv: (seen.append(argv), 0)[1]
    mod.main()
    assert seen == [["bridge", "--port", "8779"]]


def test_a_crash_holds_the_window_open_only_for_a_double_click(monkeypatch, capsys):
    """A console window that closes on a traceback takes the traceback with it. But a
    WORKER that waits for a keypress is one the studio waits out and declares failed,
    so the hold is only for the case where somebody is looking at the window."""
    mod = _launcher()
    monkeypatch.setitem(sys.modules, "pylon.cli", type(sys)("pylon.cli"))

    def boom(argv):
        raise RuntimeError("no sim")

    sys.modules["pylon.cli"].main = boom

    waited = []
    monkeypatch.setattr("builtins.input", lambda *a: waited.append(True))

    monkeypatch.setattr(sys, "argv", ["Pylon.exe", "bridge"])
    assert mod.main() == 1
    assert waited == [], "a worker must never block on a keypress"

    monkeypatch.setattr(sys, "argv", ["Pylon.exe"])
    assert mod.main() == 1
    assert waited == [True]
    assert "no sim" in capsys.readouterr().err


def test_ctrl_c_is_not_a_crash(monkeypatch):
    mod = _launcher()
    monkeypatch.setattr(sys, "argv", ["Pylon.exe"])
    monkeypatch.setitem(sys.modules, "pylon.cli", type(sys)("pylon.cli"))

    def stopped(argv):
        raise KeyboardInterrupt

    sys.modules["pylon.cli"].main = stopped
    assert mod.main() == 130


# --- the build files themselves --------------------------------------------------

@pytest.mark.parametrize("name", ["pylon.spec", "pylon.iss", "launcher.py", "README.md",
                                  "pylon.ico"])
def test_the_build_files_are_present(name):
    assert (Path(__file__).resolve().parent.parent / "packaging" / name).is_file()


def test_the_icon_carries_the_sizes_windows_actually_draws():
    """Windows picks the nearest embedded size and scales if it has to. 16 and 32 are
    what the taskbar and a Desktop shortcut use, so an icon holding only 256 looks
    like a blurry smudge in exactly the two places anyone sees it."""
    ico = (Path(__file__).resolve().parent.parent / "packaging" / "pylon.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00", "not an ICO"
    count = int.from_bytes(ico[4:6], "little")
    # Each directory entry is 16 bytes; byte 0 is the width, 0 meaning 256.
    widths = {ico[6 + i * 16] or 256 for i in range(count)}
    assert {16, 32, 256} <= widths, widths


@pytest.mark.parametrize("script", ["make_shortcut.ps1", "studio.bat", "doctor.bat"])
def test_the_source_checkout_launchers_are_present(script):
    """What a Desktop shortcut points at. Without these, running from a clone means
    remembering a command, which is the thing the shortcut exists to avoid."""
    assert (Path(__file__).resolve().parent.parent / "tools" / script).is_file()


def test_the_spec_ships_the_overlays_and_the_lazy_imports():
    """Every one of these is imported inside a function, so PyInstaller's static
    analysis cannot see it and a build without them fails only at runtime."""
    spec = (Path(__file__).resolve().parent.parent / "packaging" / "pylon.spec").read_text()
    assert '"overlays"' in spec
    for mod in ("irsdk", "obsws_python", "websockets", "tzdata"):
        assert mod in spec, mod


def test_the_installer_version_matches_the_package_version():
    """Two files name the version. They drift, and the symptom is an installer that
    quietly replaces a newer build with an older one."""
    import tomllib

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    iss = (root / "packaging" / "pylon.iss").read_text()
    want = pyproject["project"]["version"]
    assert f'#define AppVersion "{want}"' in iss, f"pyproject says {want}"
