"""Encodes the empirical finding: real driver .ibt files carry no CarIdx data.

Skips when the sample file is absent (it is gitignored, not in CI). See DESIGN.md
sections 2 and 13.
"""

import os

import pytest

IBT = os.path.join("samples", "ai_race.ibt")


@pytest.mark.skipif(not os.path.exists(IBT), reason="sample .ibt not present")
def test_ibt_has_no_caridx_channels():
    import irsdk

    ibt = irsdk.IBT()
    ibt.open(IBT)
    try:
        names = ibt.var_headers_names
        caridx = [n for n in names if n.startswith("CarIdx")]
    finally:
        ibt.close()

    assert len(names) > 0
    assert caridx == []  # driver .ibt is player-car-only; no multi-car arrays
