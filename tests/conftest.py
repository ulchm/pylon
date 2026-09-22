"""Shared test helpers.

The one rule worth stating out loud: **no test may require a capture to exist.**
`recordings/` is gitignored (the Spa captures are five and eleven megabytes), so a
CI checkout has none of them, and a test that opens one unconditionally passes on
every developer machine and fails the moment it is pushed. That is exactly how it
got through the first time.

Captures are still worth testing against (they are the only real data there is,
and DESIGN.md section 14 exists because reading `vars.txt` was not enough), so the
answer is to SKIP, not to drop the test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

RECORDINGS = Path(__file__).resolve().parent.parent / "recordings"


@pytest.fixture(autouse=True)
def _unconfigured_machine(monkeypatch):
    """Every test runs against a machine with NO PYLON_* environment.

    The settings dataclasses default their fields from those variables, and the rig is
    the one machine that has them set: the channel credentials, the Discord webhook and
    its @everyone mention live there and nowhere else. A test that describes an
    unconfigured machine (no Twitch client, no mention) passed everywhere but on the
    rig, which is where the suite has to be trustworthy after a bundle lands.
    """
    for k in [k for k in os.environ if k.startswith("PYLON_")]:
        monkeypatch.delenv(k, raising=False)


def capture_or_skip(name: str) -> str:
    """Path to a capture, or skip the calling test if this checkout has no copy."""
    path = RECORDINGS / name
    if not path.exists():
        pytest.skip(f"{name} not present (recordings/ is gitignored; CI has no captures)")
    return str(path)
