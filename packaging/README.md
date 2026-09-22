# Building the Windows release

Two artefacts come out of here: an installer for people who want one, and a zip for
people who would rather not run an installer at all.

Both have to be built **on Windows**, because PyInstaller freezes the interpreter and
the libraries of the machine it runs on. Everything else in this repository is
developed and tested on Linux; this directory is the exception.

## What you need

* Windows 10 or 11, 64-bit
* [uv](https://docs.astral.sh/uv/)
* [Inno Setup 6](https://jrsoftware.org/isdl.php), for the installer only

## Building

```
uv sync
uv run pyinstaller packaging/pylon.spec --noconfirm
```

That leaves `dist/Pylon/`, a folder holding `Pylon.exe` and everything it needs. Zip
that folder and it is the portable release: no installer, no registry, nothing outside
the folder except the settings in `%LOCALAPPDATA%\Pylon`.

For the installer, then:

```
iscc /DAppVersion=1.0.0 packaging/pylon.iss
```

which writes `packaging/Output/Pylon-Setup-<version>.exe`. The version is passed in
because `pyproject.toml` is the only place this project's version is written down;
the release workflow reads it there and hands it over. Forget the flag and the
installer is built as `0.0.0-dev`, which is deliberately obvious rather than quietly
wrong.

## Before you ship it

Run the built executable, not the source:

```
dist\Pylon\Pylon.exe doctor
dist\Pylon\Pylon.exe --help
```

`doctor` is the one that catches a bad build, because it touches the settings file,
OBS and the ports. The failure PyInstaller actually produces is a missing data file
or a missing lazy import, and both show up there rather than at import time.

Then start it with iRacing running and confirm the timing tower draws in OBS. A build
where `overlays/` did not make it into the bundle looks exactly like OBS failing to
load a browser source, and nothing before this step will tell you.

## Code signing, and why this ships unsigned

Pylon ships **unsigned**. On first download, Windows SmartScreen shows "Windows
protected your PC"; the way past it is **More info** then **Run anyway**. Some
antivirus products also flag PyInstaller output on sight, because malware authors
use PyInstaller too and the bootloader looks the same either way.

That is a real cost, and it is worth being straight with people about it in the
README rather than pretending it will not happen.

Paying a certificate authority a monthly fee to give away MIT software is a bad
trade, so if the warning becomes a problem, take one of these instead:

* **[SignPath Foundation](https://signpath.org/)** gives free code signing
  certificates to open source projects, with the signing done in their CI-integrated
  service. This is the right answer for a project like this one. It takes an
  application and a review, so start it before you need it.
* **Certum's open source developer certificate** is roughly €30 a year, which is a
  fifth of the commercial price. It arrives on a hardware token, so it cannot be
  automated in CI without a self-hosted runner.
* **Do nothing.** Plenty of well-known open source Windows tools ship unsigned and
  tell people what to expect. SmartScreen reputation does build per file hash as
  downloads accumulate, so a given release warns less over time, though a new
  release starts over.

Three things help regardless of signing, and all are already done here: the
executable carries version metadata (the spec generates `build/version_info.txt`
from `pyproject.toml` on every build), UPX is not used (the spec sets `upx=False`),
and releases come from a stable URL people can check.

If you do get a certificate, sign `dist\Pylon\Pylon.exe` **before** running `iscc`,
then sign the installer too:

```
signtool sign /fd SHA256 /tr <timestamp-url> /td SHA256 dist\Pylon\Pylon.exe
iscc packaging/pylon.iss
signtool sign /fd SHA256 /tr <timestamp-url> /td SHA256 packaging\Output\Pylon-Setup-1.0.0.exe
```

Always timestamp (`/tr`). Without it, every signature expires with the certificate
and old releases start warning again.

## The files here

* `pylon.spec`, the PyInstaller build, and `pylon.iss`, the Inno Setup installer.
* `launcher.py`, the entry point the frozen build runs.
* `version_info.py`, which generates the Windows version resource from
  `pyproject.toml` at build time. It is generated rather than committed so that it
  cannot disagree with the version in `pyproject.toml`; `tests/test_packaging.py`
  compiles its output, because a typo in it would otherwise surface only near the
  end of a Windows build.
* `pylon.ico`, the application icon. Optional: without it the build uses
  PyInstaller's default, which works and looks like nothing in particular.
