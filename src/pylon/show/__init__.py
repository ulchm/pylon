"""The layer that runs the show.

Everything below this package is a domain with a clean seam: telemetry/ reads frames,
world/ turns them into a race, director/ picks a shot, camera/ turns a shot into a
message the sim understands, obs/ switches scenes. None of them knows about the
others' processes. This package is where they are composed into a
broadcast:

  contract.py   the vocabulary that crosses process boundaries (shot kinds, flavors,
                subject keys); imports nothing, so camera/ and director/ can share it
  shotlink.py   the hand-off file between the director and the overlay
  live.py       the driver loops: run the director over a bridge (or a recording) and
                ship its cuts, replay excursions and shot echoes back
  wiring.py     how a live show is put together (the OBS hook, the replay gate, the
                replays/interrupt coupling, sources and sinks), as testable functions
  studio.py     the one-box supervisor: five workers, started in order and kept up,
                and the supervision loop `pylon studio` runs
  dock.py       the OBS control dock served over the supervisor

Deliberately no re-exports here. show/live.py imports both director/ and camera/, and
the import graph stays acyclic only because nothing below imports it back: director/
and camera/ take ShotKind from show.contract, which is a leaf. An `import
pylon.show.contract` must never execute live.py.
"""
