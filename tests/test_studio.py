"""The studio supervisor, driven entirely by fakes.

No subprocesses and no sockets: spawn, termination and the port probe are all injected,
so every case that matters is reachable without arranging for a real process to
misbehave: a worker that never binds, one that wedges while still alive, one that
dies repeatedly, a port already owned by somebody else.

The fake models the thing that makes this hard: a port opens when a healthy worker is
spawned and closes when that process dies. Pre-opening ports instead would make the
supervisor look correct while hiding the ordering entirely.
"""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from pylon.settings import SHOW
from pylon.show.dock import serve_control
from pylon.show.studio import (
    Studio,
    Worker,
    WorkerStatus,
    default_workers,
    format_event,
    format_status,
    supervise,
)

PORTS = {"bridge": 8779, "overlay": 8778}  # the director listens on nothing


class FakeProc:
    def __init__(self, pid, harness, port=None):
        self.pid = pid
        self._h = harness
        self.port = port
        self._rc = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._rc

    def _release(self):
        if self.port is not None:
            self._h.listening.discard(self.port)

    def terminate(self):
        self.terminated = True
        self._rc = -15
        self._release()

    def kill(self):
        self.killed = True
        self._rc = -9
        self._release()

    def die(self, rc=1):
        self._rc = rc
        self._release()


class Harness:
    """Fake world: spawning a healthy worker binds its port, dying releases it."""

    def __init__(self, unhealthy=(), ports=None):
        self.ports = dict(PORTS if ports is None else ports)
        self.unhealthy = set(unhealthy)  # spawns fine, never binds
        self.listening = set()
        self.spawned = []
        self.logs = []
        self.procs = []
        self.reclaimed = []
        self.orphans = set()  # ports held by nothing we own; only reclaim frees them
        self.owners = {}      # port -> [(pid, command line)], what the box can identify
        self.t = 0.0
        self.events = []

    def spawn(self, argv, cwd=None, log=None):
        name = argv[0]
        port = self.ports.get(name)
        p = FakeProc(1000 + len(self.spawned), self, port)
        self.spawned.append(list(argv))
        self.logs.append(log)
        self.procs.append(p)
        if port is not None and name not in self.unhealthy:
            self.listening.add(port)
        return p

    def reclaim(self, port):
        self.reclaimed.append(port)
        if port in self.orphans:
            self.orphans.discard(port)
            self.listening.discard(port)
            return True
        return False

    def port_owners(self, port):
        return [pid for pid, _ in self.owners.get(port, [])]

    def owner_command(self, pid):
        return next((cmd for rows in self.owners.values() for p, cmd in rows if p == pid), "")

    def terminate(self, proc):
        proc.terminate()

    def is_listening(self, port):
        return port in self.listening

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t

    def on_status(self, **kw):
        self.events.append(kw)

    def studio(self, workers, **kw):
        kw.setdefault("probe_grace", 0)  # most tests predate the grace; opt in per-test
        return Studio(workers, spawn=self.spawn, terminate=self.terminate,
                      is_listening=self.is_listening, reclaim=self.reclaim,
                      port_owners=self.port_owners, owner_command=self.owner_command,
                      sleep=self.sleep, clock=self.clock, on_status=self.on_status, **kw)


def _trio():
    return [
        Worker("bridge", ["bridge"], port=8779),
        Worker("overlay", ["overlay"], port=8778),
        Worker("director", ["director"], port=None),
    ]


def test_workers_start_in_declared_order():
    """Everything downstream dials the bridge, so order is not cosmetic."""
    h = Harness()
    s = h.studio(_trio())
    assert s.start() is True
    assert h.spawned == [["bridge"], ["overlay"], ["director"]]
    assert [r.ok for r in s.status()] == [True, True, True]


def test_a_portless_worker_is_never_probed():
    """The director only dials out. Probing a port it never opens would fail forever."""
    h = Harness()
    s = h.studio(_trio())
    assert s.start() is True
    director = next(r for r in s.status() if r.name == "director")
    assert director.listening is None and director.ok


def test_a_worker_that_never_listens_does_not_stop_the_others():
    """A worker that will not come up must not keep the others off air."""
    h = Harness(unhealthy={"overlay"})
    s = h.studio(_trio())
    assert s.start() is False
    assert h.spawned == [["bridge"], ["overlay"], ["director"]]  # director still started
    by_name = {r.name: r for r in s.status()}
    assert by_name["overlay"].ok is False
    assert by_name["bridge"].ok and by_name["director"].ok


def test_startup_says_it_exited_rather_than_that_it_never_listened():
    """Two very different next moves. Flattening them sends you hunting for a binding
    problem in a process that is not running."""
    h = Harness(unhealthy={"bridge"})
    s = h.studio([Worker("bridge", ["bridge"], port=8779, ready_timeout=1000.0)])
    inner = h.spawn

    def spawn_then_die(argv, cwd=None, log=None):
        p = inner(argv, cwd=cwd, log=log)
        p.die(1)
        return p

    s.spawn = spawn_then_die
    assert s.start() is False
    assert h.t < 1000.0  # bailed on the exit, not on the clock
    assert "exited during startup" in s.status()[0].last_error


def test_a_worker_that_stays_up_but_silent_says_it_never_listened():
    h = Harness(unhealthy={"bridge"})
    s = h.studio([Worker("bridge", ["bridge"], port=8779, ready_timeout=3.0)])
    assert s.start() is False
    assert "never listened on 8779" in s.status()[0].last_error


def test_a_port_already_in_use_is_blamed_on_the_port_not_the_worker():
    """An earlier studio that outlived its launcher owns the port. Reporting that as
    our worker failing sends you reading our logs for someone else's bug."""
    h = Harness()
    h.listening.add(8779)
    s = h.studio([Worker("bridge", ["bridge"], port=8779)])
    assert s.start() is False
    assert h.spawned == []  # never even tried to spawn
    assert "already in use" in s.status()[0].last_error


def test_a_taken_port_stays_retryable_because_it_may_just_be_lingering():
    h = Harness()
    h.listening.add(8779)
    s = h.studio([Worker("bridge", ["bridge"], port=8779)])
    s.start()
    assert s.status()[0].failed is False  # not a permanent verdict
    h.listening.discard(8779)             # the old holder finally lets go
    h.t += 60.0
    s.poll()
    assert s.status()[0].ok


def test_an_orphan_holding_the_port_is_killed_rather_than_waited_out():
    """The 2026-08-02 outage, in one test.

    A venv's Scripts\\python.exe is a redirector: kill it and the real interpreter it
    spawned lives on holding the port. Waiting cannot help, because nothing will ever
    free it, so the pre-flight has to take the port back instead of counting to five.
    """
    h = Harness(ports={"sidecar": 8780})
    # argv[0] is what the harness keys ports on; the script name is what marks it ours
    s = h.studio([Worker("sidecar", ["sidecar", "serve.py"], port=8780)])
    h.listening.add(8780)
    h.orphans.add(8780)  # only reclaim() frees it
    # the orphan is the REAL interpreter the redirector spawned: its command line is
    # the worker's, under the venv's actual python
    h.owners[8780] = [(4242, "C:/app/Scripts/../python.exe serve.py")]

    assert s.start() is True
    assert h.reclaimed == [8780]
    assert h.spawned[-1][-1] == "serve.py"  # it really did start
    assert s.status()[0].ok


def test_a_port_held_by_someone_elses_process_is_never_shot_at():
    """Reclaim is not a licence to kill whatever is on the port. On the very first
    start "our process is gone" is true of every worker, and the holder may be a
    worker someone ran by hand, a second studio, or an unrelated server. Only a
    command line that looks like one of ours is killed; the rest are named and left."""
    h = Harness()
    h.listening.add(8779)
    h.owners[8779] = [(777, "/usr/bin/node /srv/dashboard/server.js")]
    s = h.studio([Worker("bridge", ["python", "-m", "pylon", "bridge"],
                         port=8779)])
    assert s.start() is False
    assert h.reclaimed == []                       # not even attempted
    assert h.spawned == []
    err = s.status()[0].last_error
    assert "already in use" in err and "777" in err and "not one of ours" in err


def test_a_holder_the_box_cannot_identify_is_left_alone_too():
    """lsof or netstat unavailable, or a PID whose command line cannot be read: with
    no evidence either way, the safe answer is to report the port, not clear it."""
    h = Harness()
    h.listening.add(8779)                          # h.owners says nothing about it
    s = h.studio([Worker("bridge", ["bridge"], port=8779)])
    assert s.start() is False
    assert h.reclaimed == []
    assert "cannot identify" in s.status()[0].last_error


def test_a_hand_run_copy_of_our_own_worker_is_reclaimed():
    """The other side of the same rule: an `pylon overlay` left running in a terminal
    IS one of ours, and the studio is the one launcher, so it takes the port."""
    h = Harness()
    h.listening.add(8778)
    h.orphans.add(8778)
    h.owners[8778] = [(31337, "/home/dove/.venv/bin/python -m pylon overlay")]
    s = h.studio([Worker("overlay", ["overlay", "-m", "pylon"], port=8778)])
    assert s.start() is True
    assert h.reclaimed == [8778]
    reclaimed = [e for e in h.events if e.get("event") == "reclaiming"]
    assert reclaimed and reclaimed[0]["pid"] == 31337   # the log says who was killed


def test_a_live_worker_gets_grace_before_a_missed_probe_counts_as_death():
    """A worker is unreachable for the seconds it spends inside one long operation. Killing
    it for one missed probe is what turned a working worker into a restart loop."""
    h = Harness()
    s = h.studio(_trio(), probe_grace=2)
    s.start()
    h.listening.discard(8778)  # overlay busy, process still alive

    s.poll()
    s.poll()
    assert h.spawned == [["bridge"], ["overlay"], ["director"]]  # not touched yet
    assert not h.procs[1].terminated

    h.listening.add(8778)  # answered again before the grace ran out
    s.poll()
    assert h.spawned == [["bridge"], ["overlay"], ["director"]]
    assert next(r for r in s.status() if r.name == "overlay").restarts == 0


def test_the_grace_runs_out_so_a_wedged_worker_is_still_replaced():
    h = Harness()
    s = h.studio(_trio(), probe_grace=2)
    s.start()
    h.listening.discard(8778)
    for _ in range(4):
        s.poll()
    assert h.spawned[-1] == ["overlay"]
    assert h.procs[1].terminated


def test_a_dead_process_is_restarted_immediately_without_grace():
    """Grace is for a process that is alive but busy. A process that has EXITED is
    definitively dead, and making it wait extra polls for that is pure downtime."""
    h = Harness()
    s = h.studio(_trio(), probe_grace=5)
    s.start()
    h.procs[0].die()
    s.poll()
    assert h.spawned[-1] == ["bridge"]


def test_every_worker_gets_its_own_log_file():
    """Without this a crashed worker leaves no trace anywhere: CREATE_NO_WINDOW gives
    each one its own INVISIBLE console, so its traceback goes to a buffer nobody can
    read."""
    h = Harness()
    s = h.studio(_trio(), log_dir="/logs")
    s.start()
    # Compared as paths, not strings: the studio builds these with pathlib, so the
    # separator is the host's and a literal "/logs/bridge.log" fails on the rig.
    assert [Path(p) for p in h.logs] == [Path("/logs") / f"{n}.log"
                                         for n in ("bridge", "overlay", "director")]


def test_no_log_dir_means_no_redirection():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    assert h.logs == [None, None, None]


def test_poll_restarts_a_worker_that_died():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    h.procs[0].die()

    rows = s.poll()
    assert len(h.spawned) == 4 and h.spawned[-1] == ["bridge"]
    assert next(r for r in rows if r.name == "bridge").restarts == 1
    assert next(r for r in rows if r.name == "bridge").ok  # and it is serving again


def test_poll_restarts_a_worker_that_is_alive_but_has_stopped_serving():
    """The case a PID check misses completely: wedged, holding its process open,
    answering nothing. This is the one that actually happens."""
    h = Harness()
    s = h.studio(_trio())
    s.start()
    h.listening.discard(8778)  # overlay wedges without exiting

    s.poll()
    assert h.spawned[-1] == ["overlay"]
    # it had to be killed first, or the replacement could not bind the port
    assert h.procs[1].terminated


def test_restart_backs_off_and_eventually_gives_up():
    h = Harness(unhealthy={"bridge"})
    s = h.studio([Worker("bridge", ["bridge"], port=8779, ready_timeout=0.0)],
                 max_consecutive_failures=3)
    s.start()

    before = len(h.spawned)
    for _ in range(40):
        s.poll()
        h.t += 60.0  # jump past any backoff
    row = s.status()[0]
    assert row.failed is True and "gave up after 3" in row.last_error
    assert len(h.spawned) - before == 3  # and then it stops, however often we poll


def test_backoff_actually_delays_the_next_attempt():
    h = Harness(unhealthy={"bridge"})
    s = h.studio([Worker("bridge", ["bridge"], port=8779, ready_timeout=0.0)],
                 backoff_base=5.0)
    s.start()
    n = len(h.spawned)
    s.poll()
    assert len(h.spawned) == n + 1
    s.poll()                     # too soon
    assert len(h.spawned) == n + 1
    h.t += 5.0
    s.poll()
    assert len(h.spawned) == n + 2


def test_a_healthy_pass_clears_the_failure_count():
    h = Harness()
    s = h.studio(_trio(), max_consecutive_failures=2)
    s.start()
    h.procs[0].die()
    s.poll()
    h.t += 60.0
    for _ in range(5):
        s.poll()
    assert s.status()[0].failed is False


def test_stop_terminates_everything_and_frees_the_ports():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    s.stop()
    assert all(p.terminated for p in h.procs)
    assert h.listening == set()
    assert all(r.pid is None and not r.alive for r in s.status())


def test_stopping_goes_through_the_tree_killer_not_proc_terminate():
    """On Windows a venv launcher is a REDIRECTOR and the real interpreter is its
    child, holding the socket. Reaping only the launcher orphans the worker, which
    keeps the port and makes every later restart impossible."""
    h = Harness()
    s = h.studio(_trio())
    killed = []
    s.terminate = lambda p: (killed.append(p.pid), p.terminate())
    s.start()
    s.stop()
    assert len(killed) == 3


def test_a_wedged_worker_is_also_killed_as_a_tree():
    h = Harness()
    s = h.studio(_trio())
    killed = []
    s.terminate = lambda p: (killed.append(p.pid), p.terminate())
    s.start()
    h.listening.discard(8778)
    s.poll()
    assert killed == [h.procs[1].pid]


def test_stop_kills_what_will_not_terminate():
    h = Harness()
    s = h.studio([Worker("stubborn", ["bridge"], port=8779)])
    s.start()
    proc = h.procs[0]
    s.terminate = lambda p: None  # ignores the polite request
    s.stop(timeout=1.0)
    assert proc.killed


def test_spawn_failure_is_recorded_not_raised():
    h = Harness()
    s = h.studio(_trio())

    def boom(argv, cwd=None, log=None):
        raise OSError("no such file")

    s.spawn = boom
    assert s.start() is False
    assert all(r.failed for r in s.status())
    assert "no such file" in s.status()[0].last_error


# --- a restart must not stop the world ----------------------------------------

def test_a_restart_does_not_block_supervision_of_the_others():
    """poll() used to hold the supervisor inside _await_port for a whole ready_timeout
    (up to 180s for a slow one), blind to every other worker and, through a shared
    lock, to the dock's status() as well. A restarted worker is now `starting`, and
    the pass moves on."""
    h = Harness(unhealthy={"overlay"})
    s = h.studio(_trio())
    s.start()                                       # overlay never comes up
    h.t += 60.0
    before = h.t
    s.poll()                                        # relaunches the overlay
    assert h.t == before                            # without waiting on it
    assert next(r for r in s.status() if r.name == "overlay").starting

    h.procs[0].die()                                # meanwhile the bridge dies
    s.poll()
    assert h.spawned[-1] == ["bridge"]              # and is seen to, at once
    assert h.t == before


def test_a_starting_worker_becomes_ready_on_a_later_pass():
    h = Harness(unhealthy={"overlay"})
    s = h.studio(_trio())
    s.start()
    h.t += 60.0
    s.poll()                                        # relaunch: still unhealthy
    h.unhealthy.clear()
    h.listening.add(8778)                           # it answers a moment later
    rows = s.poll()
    overlay = next(r for r in rows if r.name == "overlay")
    assert overlay.ok and not overlay.starting
    assert any(e.get("event") == "ready" and e.get("worker") == "overlay"
               for e in h.events[-3:])


def test_a_restart_that_never_answers_runs_out_of_time_and_backs_off():
    h = Harness(unhealthy={"overlay"})
    s = h.studio([Worker("overlay", ["overlay"], port=8778, ready_timeout=30.0)],
                 max_consecutive_failures=2)
    s.start()
    h.t += 60.0
    s.poll()                                        # relaunch #1, starting
    n = len(h.spawned)
    h.t += 10.0
    s.poll()                                        # still within its 30s: nothing
    assert len(h.spawned) == n
    h.t += 30.0
    s.poll()                                        # timed out -> failure #2 -> relaunch
    assert "never listened" in s.status()[0].last_error or s.status()[0].failed
    h.t += 60.0
    s.poll()
    h.t += 60.0
    s.poll()
    assert s.status()[0].failed


def test_the_dock_reports_a_starting_worker_as_starting():
    h = Harness(unhealthy={"overlay"})
    s = h.studio(_trio())
    s.start()
    h.t += 60.0
    s.poll()
    srv, port = _dock(s)
    try:
        data = json.loads(_get(port, "/api/status")[1])
        overlay = next(w for w in data["workers"] if w["name"] == "overlay")
        assert overlay["starting"] is True and overlay["ok"] is False
    finally:
        srv.shutdown()


# --- the default set ---------------------------------------------------------

def test_default_workers_are_in_dependency_order():
    ws = default_workers(python="PY")
    assert [w.name for w in ws] == ["bridge", "overlay", "director"]
    # Against the SETTINGS, not a hardcoded number: an operator's config moves these,
    # and what this test is about is the order and the portless director.
    assert ws[0].port == SHOW.bridge_port and ws[-1].port is None


def test_every_worker_runs_under_the_interpreter_it_was_given():
    assert all(w.argv[0] == "PY" for w in default_workers(python="PY"))


def test_instant_replays_are_off_unless_asked_for():
    """And when they are off, nothing anywhere says so: the only symptom is that no
    replay ever happens, which is exactly how it went unnoticed through a whole race."""
    director = next(w for w in default_workers(python="PY") if w.name == "director")
    assert "--replays" not in director.argv


def test_the_studio_can_turn_instant_replays_on():
    director = next(w for w in default_workers(python="PY", replays=True)
                    if w.name == "director")
    assert "--replays" in director.argv


def test_replay_swaps_the_live_sim_for_a_recording():
    bridge = default_workers(python="PY", replay="rec.jsonl.gz")[0].argv
    assert "--replay" in bridge and "rec.jsonl.gz" in bridge
    assert "--live" not in bridge and "--sdk" not in bridge


def test_live_is_the_default_when_no_recording_is_given():
    bridge = default_workers(python="PY")[0].argv
    assert "--sdk" in bridge and "--live" in bridge and "--replay" not in bridge


def test_everything_downstream_dials_the_bridge_port_it_was_given():
    ws = default_workers(python="PY", bridge_port=9999)
    assert "9999" in ws[0].argv
    for w in ws:
        if w.name in ("overlay", "director"):
            assert any("ws://127.0.0.1:9999" in a for a in w.argv)


# --- requests from the dock --------------------------------------------------

def test_an_unknown_worker_is_refused_so_the_dock_can_say_so():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    assert s.request_restart("nope") is False
    assert s.request_restart("overlay") is True


def test_a_requested_restart_replaces_the_running_worker():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    s.request_restart("overlay")
    assert s.drain_requests() == ["overlay"]
    assert h.spawned[-1] == ["overlay"]
    assert h.procs[1].terminated  # the old one went first, as a tree


def test_a_manual_restart_still_moves_the_counter():
    """The dock shows a status word and this number and no pid, so restarting a HEALTHY
    worker would otherwise look identical afterwards: the operator clicks, the process
    really is replaced, and nothing on screen changes."""
    h = Harness()
    s = h.studio(_trio())
    s.start()
    assert next(r for r in s.status() if r.name == "overlay").restarts == 0
    s.request_restart("overlay")
    s.drain_requests()
    assert next(r for r in s.status() if r.name == "overlay").restarts == 1


def test_a_requested_restart_revives_a_worker_that_had_been_given_up_on():
    """The operator asking IS the new information. A worker you cannot retry from the
    dock is exactly the one you end up restarting the whole studio for."""
    h = Harness(unhealthy={"overlay"})
    s = h.studio(_trio(), max_consecutive_failures=1)
    s.start()
    for _ in range(6):
        s.poll()
        h.t += 60.0
    assert next(r for r in s.status() if r.name == "overlay").failed is True

    h.unhealthy.clear()  # whatever was wrong is fixed
    s.request_restart("overlay")
    s.drain_requests()
    assert next(r for r in s.status() if r.name == "overlay").ok


def test_stop_is_a_request_not_an_action():
    """The dock runs on another thread; it must not tear workers down underneath the
    supervision loop. It asks, and the loop notices."""
    h = Harness()
    s = h.studio(_trio())
    s.start()
    assert s.stop_requested is False
    s.request_stop()
    assert s.stop_requested is True
    assert all(r.alive for r in s.status())  # nothing torn down yet


# --- the control dock over real HTTP -----------------------------------------

def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read()


def _post(port, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _dock(studio):
    srv = serve_control(studio, host="127.0.0.1", port=0)
    return srv, srv.server_address[1]


def test_the_dock_reports_every_worker_and_whether_it_is_serving():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    srv, port = _dock(s)
    try:
        code, body = _get(port, "/api/status")
        data = json.loads(body)
        assert code == 200
        assert [w["name"] for w in data["workers"]] == ["bridge", "overlay", "director"]
        assert all(w["ok"] for w in data["workers"])
        # the portless director is distinguishable from one that is failing to listen
        assert data["workers"][2]["listening"] is None
        assert data["stopping"] is False
    finally:
        srv.shutdown()


def test_the_dock_can_restart_a_worker_and_refuses_an_unknown_one():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    srv, port = _dock(s)
    try:
        code, body = _post(port, "/api/restart", {"worker": "overlay"})
        assert code == 200 and body["ok"]
        assert s.drain_requests() == ["overlay"]

        code, body = _post(port, "/api/restart", {"worker": "ghost"})
        assert code == 404 and "ghost" in body["error"]
    finally:
        srv.shutdown()


def test_the_dock_stop_button_only_asks():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    srv, port = _dock(s)
    try:
        code, body = _post(port, "/api/stop")
        assert code == 200 and body["stopping"]
        assert s.stop_requested
        assert all(r.alive for r in s.status())  # the loop does the tearing down
        assert json.loads(_get(port, "/api/status")[1])["stopping"] is True
    finally:
        srv.shutdown()


def test_a_malformed_restart_body_is_a_400_not_a_traceback():
    h = Harness()
    s = h.studio(_trio())
    s.start()
    srv, port = _dock(s)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/restart",
                                     data=b"{not json", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("should have been a 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.shutdown()


def test_an_unknown_endpoint_is_a_404():
    h = Harness()
    s = h.studio(_trio())
    srv, port = _dock(s)
    try:
        assert _post(port, "/api/launch-the-missiles")[0] == 404
    finally:
        srv.shutdown()


def test_the_dock_page_is_actually_shipped_and_served_at_the_root():
    """Guards the boring failure where the API works and the panel 404s."""
    assert (SHOW.overlays_dir / "control.html").is_file()
    h = Harness()
    s = h.studio(_trio())
    srv, port = _dock(s)
    try:
        code, body = _get(port, "/")
        assert code == 200 and b"/api/status" in body
    finally:
        srv.shutdown()


# --- reporting ---------------------------------------------------------------

def test_format_status_distinguishes_the_failure_modes():
    rows = [
        WorkerStatus("bridge", 1, True, True, 0, False, ""),
        WorkerStatus("overlay", 2, True, False, 3, False, ""),
        WorkerStatus("overlay", None, False, False, 1, False, "exited"),
        WorkerStatus("director", None, False, False, 5, True, "gave up after 5"),
        WorkerStatus("director", 4, True, None, 0, False, ""),
    ]
    out = format_status(rows)
    assert "bridge" in out and "ok" in out
    assert "alive but not serving" in out          # the wedged case is called out
    assert "FAILED" in out and "gave up after 5" in out
    assert "no port to probe" in out               # not mistaken for broken
    assert "restarts 3" in out


# --- the supervision loop ------------------------------------------------------
# What `pylon studio` runs after the workers are declared. Driven here with a fake
# sleep that performs one scripted action per pass, so the loop can be watched without
# waiting for it.

def _supervise(h, s, *, script):
    lines = []
    passes = iter(script)

    def sleep(_):
        action = next(passes, None)
        if action is not None:
            action()

    supervise(s, interval=1.0, emit=lines.append, sleep=sleep)
    return lines


def _tables(lines):
    return [ln for ln in lines if "bridge" in ln and "overlay" in ln and "director" in ln]


def test_supervise_prints_the_table_at_startup_and_then_only_on_a_change():
    """A status line every few seconds is noise that trains you to ignore the one line
    that matters. Here the overlay dies once; the table appears twice in total."""
    h = Harness()
    s = h.studio(_trio())
    lines = _supervise(h, s, script=[lambda: None, lambda: h.procs[1].die(), lambda: None,
                                     lambda: None, s.request_stop])
    tables = _tables(lines)
    assert len(tables) == 2, lines
    assert "restarts 1" in tables[1]
    assert lines[-2:] == ["[studio] stop requested from the panel", "[studio] all stopped"]


def test_supervise_relays_a_restart_asked_for_from_the_dock():
    h = Harness()
    s = h.studio(_trio())
    lines = _supervise(h, s, script=[lambda: s.request_restart("overlay"), s.request_stop])
    assert "[studio] overlay: restart requested from the panel" in lines


def test_supervise_stops_every_worker_on_ctrl_c():
    h = Harness()
    s = h.studio(_trio())

    def ctrl_c():
        raise KeyboardInterrupt

    lines = _supervise(h, s, script=[ctrl_c])
    assert lines[-1] == "[studio] all stopped"
    assert all(p.terminated or p.killed for p in h.procs)


def test_supervise_takes_the_dock_down_with_the_studio():
    class Dock:
        down = False

        def shutdown(self):
            self.down = True

    h = Harness()
    s = h.studio(_trio())
    dock = Dock()
    supervise(s, interval=1.0, emit=lambda _: None, dock=dock, sleep=lambda _: s.request_stop())
    assert dock.down
    assert all(p.terminated or p.killed for p in h.procs)


def test_supervise_says_so_when_not_everything_came_up():
    h = Harness(unhealthy={"overlay"})
    s = h.studio([Worker("overlay", ["overlay"], port=8778, ready_timeout=1.0)])
    lines = _supervise(h, s, script=[s.request_stop])
    assert "[studio] not everything came up - see below" in lines


def test_format_event_drops_empty_fields():
    assert format_event(worker="overlay", event="restarted", attempt=2, backoff=None,
                        command="") == "[studio] overlay: restarted  attempt=2"


# --- publishing the titles ---------------------------------------------------

class _Report:
    def __init__(self, lines, ok=True):
        self.lines, self.ok = lines, ok


def test_obs_setup_runs_on_a_thread_and_the_state_records_the_outcome():
    """It dials obs-websocket, which can take a connect timeout to fail, so neither
    the bring-up nor the supervision loop may wait on it."""
    import threading
    h = Harness()
    gate = threading.Event()
    seen = []

    def setup():
        gate.wait(5)
        return _Report(["Scene 'iRacing - Broadcast' is ready", "  overlay   Overlay"])

    s = h.studio(_trio(), obs_setup=setup)
    assert s.request_obs_setup(on_done=seen.append)
    assert s.setup_state["running"] and s.setup_state["ok"] is None
    assert not s.request_obs_setup()          # one at a time
    gate.set()
    deadline = time.monotonic() + 5
    while s.setup_state["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.setup_state["ok"] is True
    assert s.setup_state["lines"][-1] == "  overlay   Overlay"
    assert seen and seen[0].ok


def test_a_job_that_raises_is_reported_not_lost():
    """The thread must report, not die silently: a button that does nothing and says
    nothing is the worst outcome for the one surface an operator is looking at."""
    h = Harness()

    def setup():
        raise RuntimeError("OBS is not running")

    s = h.studio(_trio(), obs_setup=setup)
    s.request_obs_setup()
    deadline = time.monotonic() + 5
    while s.setup_state["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.setup_state["ok"] is False
    assert s.setup_state["lines"] == ["FAILED, OBS is not running"]


def test_the_panel_can_ask_for_an_obs_setup_and_sees_it_in_status():
    import threading
    h = Harness()
    gate = threading.Event()
    s = h.studio(_trio(), obs_setup=lambda: (gate.wait(5), _Report(["done"]))[1])
    srv, port = _dock(s)
    try:
        code, body = _post(port, "/api/obs-setup")
        assert code == 202 and body["running"]
        code, body = _post(port, "/api/obs-setup")
        assert code == 409
        _, status = _get(port, "/api/status")
        assert json.loads(status)["setup"]["running"] is True
        gate.set()
        deadline = time.monotonic() + 5
        while s.setup_state["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        _, status = _get(port, "/api/status")
        assert json.loads(status)["setup"] == {"running": False, "ok": True,
                                               "lines": ["done"],
                                               "at": s.setup_state["at"]}
    finally:
        srv.shutdown()


def test_the_supervisor_notices_the_stream_starting_and_lights_the_panel():
    """The on-air watch is ticked by the loop. An off-to-on edge says so once, and
    the state stays readable afterwards, which is what the panel's LIVE lamp is."""
    from pylon.show.onair import OnAir
    from pylon.show.studio import supervise

    h = Harness()
    s = h.studio(_trio())
    for w in s.workers:
        h.listening.add(w.port) if w.port else None
    answers = iter([False, True] + [True] * 50)
    clock = {"t": 0.0}
    onair = OnAir(lambda: next(answers), lambda: None, every=0, clock=lambda: clock["t"])
    lines = []

    def sleep(_):
        clock["t"] += 1
        if onair.went_live:
            s.request_stop()

    supervise(s, interval=0, emit=lines.append, sleep=sleep, onair=onair)
    assert any("you are live" in ln for ln in lines)
    assert s.streaming is True
