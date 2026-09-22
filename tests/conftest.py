"""Shared test helpers.

**Every test runs against an UNCONFIGURED machine**, and the two lines that make
that true have to run before anything imports pylon, which is why they are at
module scope rather than in a fixture. See `_ISOLATED_CONFIG` below.

The other rule worth stating out loud: **no test may require a capture to exist.**
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

# --- an unconfigured machine, decided before pylon is imported ----------------
#
# `settings.SHOW` is built from the operator's config.toml AT IMPORT, and modules
# then capture its values as argparse defaults and as default arguments. So by the
# time any fixture runs it is far too late: the numbers are already baked in.
#
# pytest imports conftest before it imports a single test module, and this module
# imports no pylon, so pointing PYLON_CONFIG at a path that cannot exist here is
# what guarantees every test sees the defaults.
#
# The failure it stops is specific and was live on 2026-09-22: the sim rig's own
# config moves the ports into the 887x range to coexist with another broadcaster,
# and `test_default_workers_are_in_dependency_order` asserted the default 8779. It
# passed on every developer machine and failed the moment it ran on the one box the
# suite most needs to be trustworthy on.
_ISOLATED_CONFIG = Path(__file__).resolve().parent / "no-such-config.toml"
assert not _ISOLATED_CONFIG.exists(), f"{_ISOLATED_CONFIG} must not exist"
os.environ["PYLON_CONFIG"] = str(_ISOLATED_CONFIG)


@pytest.fixture(autouse=True)
def _unconfigured_machine(monkeypatch):
    """No PYLON_* environment either, for the same reason as the config above.

    PYLON_CONFIG is the exception and is re-set rather than deleted: it is how this
    suite pins itself to a machine with no settings, so removing it would hand every
    test the real operator's config, which is the thing being guarded against. A test
    that wants its own config file monkeypatches it, which still works.
    """
    for k in [k for k in os.environ if k.startswith("PYLON_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PYLON_CONFIG", str(_ISOLATED_CONFIG))


def capture_or_skip(name: str) -> str:
    """Path to a capture, or skip the calling test if this checkout has no copy."""
    path = RECORDINGS / name
    if not path.exists():
        pytest.skip(f"{name} not present (recordings/ is gitignored; CI has no captures)")
    return str(path)
