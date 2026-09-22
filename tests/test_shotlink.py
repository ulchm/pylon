"""The shot file: the director's hand-off to the overlay."""

from pylon.director.model import Shot, ShotKind
from pylon.show import shotlink


def test_a_replay_can_be_published_before_the_opening_shot(tmp_path, monkeypatch):
    """A candidate can be armed before the director has cut at all (everyone pitted
    under yellow, then contact). The writer is documented as never raising into the
    loop, and it must not: the file carries the replay block and no shot."""
    monkeypatch.setattr(shotlink, "SHOT_PATH", tmp_path / "shot.json")
    shotlink.write_shot(None, replay={"phase": "armed", "key": "contact:3:100.0"})
    d = shotlink.read_shot()
    assert d is not None and d["target"] is None and d["kind"] is None
    assert d["replay"]["phase"] == "armed"

    shotlink.write_shot(Shot(ShotKind.LEADER, "leader:3", 3, "#3"))
    assert shotlink.read_shot()["target"] == 3


def test_a_read_that_loses_a_race_with_the_writer_holds_the_last_good_record(tmp_path, monkeypatch):
    """Round 1, 2026-09-20: the director rewrites the file every frame of a replay,
    one read per excursion failed inside the swap, and the overlay took "no record"
    for "no replay" and wiped out of the tape and straight back in. A failed read
    within a second of a good one is the good one. Staleness is not a glitch."""
    path = tmp_path / "shot.json"
    monkeypatch.setattr(shotlink, "SHOT_PATH", path)
    shotlink._last_good = None
    shotlink.write_shot(Shot(ShotKind.LEADER, "leader:3", 3, "#3"),
                        replay={"active": True, "phase": "rolling", "key": "trouble:3:10.0"})
    assert shotlink.read_shot()["replay"]["active"] is True

    # the swap in progress: for one read the file is not there / not readable
    monkeypatch.setattr(type(path), "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError(32, "in use")))
    d = shotlink.read_shot()
    assert d is not None and d["replay"]["key"] == "trouble:3:10.0"
    monkeypatch.undo()
    monkeypatch.setattr(shotlink, "SHOT_PATH", path)

    # a half-written record is the same glitch
    path.write_text('{"kind": "leader", "tar')
    assert shotlink.read_shot()["replay"]["key"] == "trouble:3:10.0"

    # but the director gone quiet is not: past STALE_AFTER the answer is None again,
    # and the held record goes with it
    import os
    import time
    old = time.time() - shotlink.STALE_AFTER - 1
    shotlink.write_shot(Shot(ShotKind.LEADER, "leader:3", 3, "#3"))
    os.utime(path, (old, old))
    assert shotlink.read_shot() is None
    path.write_text("not json")
    assert shotlink.read_shot() is None

    # and a good read past READ_GRACE is not covered by an old one either
    shotlink.write_shot(Shot(ShotKind.LEADER, "leader:3", 3, "#3"))
    assert shotlink.read_shot()["target"] == 3
    shotlink._last_good = (time.time() - shotlink.READ_GRACE - 0.1, shotlink._last_good[1])
    path.write_text("not json")
    assert shotlink.read_shot() is None
