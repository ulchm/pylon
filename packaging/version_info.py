"""The Windows version resource, built from the one version in pyproject.toml.

Kept out of `pylon.spec` and in a module of its own so it can be imported and tested
on any platform. The resource file is Python that PyInstaller evaluates with its own
classes in scope, so a typo in it is only discovered when a Windows build runs, which
is the slowest possible place to find one: `tests/test_packaging.py` compiles it here
instead.

Generated rather than committed, because a version number written down twice is a
version number that goes stale, and the one people would notice is an installer
claiming a version the application it installs does not have.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def read_version(root: Path) -> str:
    """The project's version, from the only place it is written down."""
    with (root / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def version_quad(version: str) -> tuple[int, int, int, int]:
    """Windows wants exactly four numbers; a version like "1.2" or "1.2.3rc1" does not
    have them. Take the leading numeric parts and pad with zeros, so a pre-release tag
    cannot stop a build."""
    parts: list[int] = []
    for piece in version.split(".")[:4]:
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]


def version_info_source(version: str) -> str:
    """The resource file's text, in PyInstaller's own format."""
    quad = version_quad(version)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Pylon'),
          StringStruct('FileDescription', 'Automatic iRacing broadcast director'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'Pylon'),
          StringStruct('LegalCopyright', 'MIT licence'),
          StringStruct('OriginalFilename', 'Pylon.exe'),
          StringStruct('ProductName', 'Pylon'),
          StringStruct('ProductVersion', '{version}'),
        ],
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""


def write_version_info(root: Path, version: str) -> str:
    """Write the resource under `build/` and return its path."""
    out = root / "build"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "version_info.txt"
    path.write_text(version_info_source(version), encoding="utf-8")
    return str(path)
