# PyInstaller spec for Pylon. Build on Windows:
#
#     uv sync
#     uv run pyinstaller packaging/pylon.spec --noconfirm
#
# Produces dist/Pylon/, a folder with Pylon.exe and everything it needs. That folder
# is what the Inno Setup script installs and what the portable zip contains.
#
# Three decisions worth knowing about.
#
# ONEDIR, NOT ONEFILE. A onefile build unpacks itself into a temporary directory on
# every launch, which costs seconds on a cold disk and, worse, makes `sys.executable`
# useless for finding our own files: the studio spawns workers by re-running itself,
# and under onefile each worker would unpack its own copy of the whole application.
# Onedir starts instantly and every process shares one tree.
#
# THE OVERLAYS SHIP AS DATA, NOT CODE. They are HTML that OBS loads over HTTP from
# our own server, so they must exist as real files on disk. settings._overlays_dir
# finds them next to the executable; getting that wrong ships an installer whose
# overlay serves 404s, which looks exactly like OBS failing to load a browser source.
#
# CONSOLE, NOT WINDOWED. The supervision log is the only place a worker that will not
# start explains itself, and a solo operator with a black console window has something
# to read and to paste into a bug report. The installer's shortcut is what decides
# whether anyone has to look at it.

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent

datas = [
    # The browser sources OBS loads: the timing tower, the holding cards, the panel.
    (str(ROOT / "overlays"), "overlays"),
]

# Windows-only, and imported lazily inside functions, so PyInstaller's static analysis
# never sees them. Named here or the frozen build has no way to read the sim at all.
hiddenimports = [
    "irsdk",
    "obsws_python",
    "websockets",
    "yaml",
    # Windows Python ships no zoneinfo database.
    "tzdata",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here needs a GUI toolkit or a scientific stack. Excluding them keeps the
    # build to tens of megabytes instead of hundreds, which matters when the thing
    # being downloaded is aimed at people on home connections.
    excludes=[
        "tkinter", "matplotlib", "numpy", "scipy", "PIL", "pandas",
        "pytest", "IPython", "torch", "torchaudio", "transformers",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Pylon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed executables are a reliable antivirus false positive
    console=True,
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "pylon.ico")
         if (ROOT / "packaging" / "pylon.ico").exists() else None,
    version=str(ROOT / "packaging" / "version_info.txt")
            if (ROOT / "packaging" / "version_info.txt").exists() and sys.platform == "win32"
            else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Pylon",
)
