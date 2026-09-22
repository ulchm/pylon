"""The studio supervisor: one thing to launch, which owns everything else.

The broadcast is three long-running processes (bridge, overlay, director) that have to
come up in the right order and stay up. Doing that by hand is three terminals and a
memorised order; this is the one launcher instead.

Three decisions worth knowing about.

**Liveness is a PORT, not a PID.** A process being alive says nothing about whether it
is serving. Measured on the rig on 2026-07-31: a server started over ssh was reported
running and had already been killed with the session, and the only symptom downstream
was OBS rendering a blank page. So a worker that declares a port is not "up" until
something accepts a connection on it, and the supervisor keeps checking, not just at
startup. A worker with no port (the director, which only makes outbound connections)
falls back to liveness, which is the best that can be known about it.

**Start in order, and WAIT.** Everything downstream dials the bridge. Starting them
all at once means a thundering herd of failed connections and workers that exit before
their dependency exists. Each one has to be answering before the next is spawned.

**Restart, but give up eventually.** A worker that dies once is usually a blip worth
retrying with a backoff. A worker that dies five times in a row is misconfigured, and
retrying forever turns one broken thing into an unreadable status display and a log
nobody can search. It gets marked failed and left alone, still visible in status().

Process spawning and the port probe are both injected, so the whole state machine is
testable on Linux with no subprocesses and no sockets. The OBS control panel that sits
on top of this is dock.py.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..settings import SHOW, ShowSettings


@dataclass(frozen=True)
class Worker:
    """One process the studio owns.

    `port` is what proves it is serving. None means there is nothing to dial, so
    liveness is all we get: true of the director, which only makes outbound
    connections and never listens.
    """

    name: str
    argv: Sequence[str]
    port: int | None = None
    ready_timeout: float = 40.0
    cwd: str | None = None


@dataclass
class WorkerState:
    worker: Worker
    proc: object | None = None
    restarts: int = 0
    consecutive_failures: int = 0
    probe_misses: int = 0         # consecutive missed port probes while still alive
    failed: bool = False          # gave up; no longer being retried
    last_error: str = ""
    next_retry_at: float = 0.0
    starting: bool = False        # spawned, not yet answering its port (see _launch)
    start_deadline: float = 0.0   # when `starting` becomes "never listened"

    @property
    def name(self) -> str:
        return self.worker.name


@dataclass
class WorkerStatus:
    name: str
    pid: int | None
    alive: bool
    listening: bool | None        # None when the worker declares no port
    restarts: int
    failed: bool
    last_error: str
    starting: bool = False

    @property
    def ok(self) -> bool:
        """Serving if it can be, running if it cannot."""
        if self.failed or not self.alive:
            return False
        return self.listening is not False


def port_open(port: int, host: str = SHOW.loopback, timeout: float = 0.4) -> bool:
    """True if something accepts a TCP connection. The only honest liveness check."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def kill_tree(proc) -> None:
    r"""Stop a worker and everything it spawned.

    On Windows a venv's `Scripts\python.exe` is a REDIRECTOR: it launches the real
    interpreter as a child, and the child is what holds the socket. Measured on the rig
    2026-07-31: every worker was two processes, e.g. the bridge at pid 14228 with the
    interpreter doing the work at 10404. So `terminate()` reaps the redirector and
    orphans the process actually serving, which keeps the port bound; the replacement
    can then never bind, and the studio reports a worker that will not come up while
    the old one is answering perfectly well.

    `taskkill /T` walks the tree, and has to run while the tree is INTACT: once the
    parent is reaped the child cannot be reached by walking anymore.

    Under ssh none of this shows, because Windows OpenSSH puts the session in a job
    object that cascades the kill: the same thing that kills servers started over
    ssh. A studio launched from a desktop shortcut, which is how this is meant to run,
    has no such job object. POSIX needs none of it: no redirector, one process.
    """
    pid = getattr(proc, "pid", None)
    if sys.platform == "win32" and pid:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, check=False)
        return
    try:
        proc.terminate()
    except OSError:
        pass


LOG_ROTATE_BYTES = 5_000_000


def rotate_log(path: Path, limit: int = LOG_ROTATE_BYTES) -> None:
    """Keep one previous generation once a log passes `limit` bytes."""
    try:
        if path.stat().st_size > limit:
            path.replace(path.with_suffix(path.suffix + ".old"))
    except OSError:
        pass  # no log yet, or another process holds it; appending is still fine


def _open_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(path)
    # Not a context manager on purpose: this handle is handed to Popen, and the
    # caller closes its own copy once the child has dup'd it.
    fh = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    fh.write(f"\n=== spawned {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    fh.flush()
    return fh


def _spawn(argv: Sequence[str], cwd: str | None = None, log: str | None = None):
    """Start a worker detached from any console window, with its output on disk.

    CREATE_NO_WINDOW matters on Windows for exactly one reason: without it, launching
    the studio from a desktop shortcut throws up five console windows alongside it,
    which is the thing this whole module exists to avoid.

    It also has a consequence that cost a whole race to discover: CREATE_NO_WINDOW does
    not mean "no console", it means "a console with no WINDOW". Each worker got its own
    invisible conhost, so every traceback a worker ever printed went into a buffer
    nobody can read, and the studio's own console is 30 lines deep, so by the time
    anyone looks, the one message explaining the crash is long gone. Hence `log`:
    stdout and stderr go to a file that outlives the process that wrote it.
    """
    kwargs: dict = {"cwd": cwd}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    fh = None
    if log is not None:
        fh = _open_log(Path(log))
        kwargs["stdout"] = fh
        kwargs["stderr"] = subprocess.STDOUT
        # A child writing to a FILE block-buffers by default, so a crash loses the last
        # and most interesting few KB. At this volume unbuffered costs nothing.
        kwargs["env"] = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        return subprocess.Popen(list(argv), **kwargs)
    finally:
        # Popen dups the handle into the child, so the parent's copy is dead weight,
        # and holding it would keep the file open across every future restart.
        if fh is not None:
            fh.close()


def port_owner_pids(port: int) -> list[int]:
    """PIDs listening on `port`, best effort. Empty when we cannot tell."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, timeout=10, check=False).stdout
            pids = []
            for line in out.splitlines():
                f = line.split()
                if len(f) >= 5 and f[3].upper() == "LISTENING" and re.search(
                        rf"[:.]{port}$", f[1]):
                    pids.append(int(f[-1]))
            return sorted(set(pids))
        out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10,
                             check=False).stdout
        return sorted({int(x) for x in out.split()})
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def process_command(pid: int) -> str:
    """The command line of a process, best effort. Empty when we cannot tell.

    This is what stands between "take the port back from our own orphan" and "kill
    whatever is on that port": the studio only reclaims from a holder whose command
    line looks like one of its workers (see Studio._looks_like_ours).
    """
    try:
        if sys.platform == "win32":
            # Not wmic: it is gone from current Windows builds. PowerShell is not.
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 (f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
                  ".CommandLine")],
                capture_output=True, text=True, timeout=15, check=False).stdout
            return out.strip()
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        return " ".join(p.decode(errors="replace") for p in raw.split(b"\0") if p)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def reclaim_port(port: int) -> bool:
    """Kill whatever is listening on `port`. True if we killed something.

    This exists for one specific, repeatedly observed failure: a venv's
    `Scripts\\python.exe` is a REDIRECTOR that runs the real interpreter as a child, and
    the CHILD is what holds the socket. Kill the launcher and the child survives, owning
    a port its supervisor no longer knows about. Nothing will ever free it, so every
    restart fails the "port already in use" pre-flight, five times, and the worker is
    given up on while a zombie holds its socket. Measured live on 2026-08-02, when one
    worker reached 53 restarts this way inside a single race.
    """
    killed = False
    for pid in port_owner_pids(port):
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
            else:
                os.kill(pid, 9)
            killed = True
        except (OSError, subprocess.SubprocessError):
            pass
    return killed


def _default_obs_setup():
    """Build the OBS scenes: what the control panel's "Set up OBS" button runs.

    Imported late and called on a thread, because it dials obs-websocket and an OBS
    that is not there takes a connect timeout to say so. The panel reads the result
    out of `setup_state` the same way it reads worker status.
    """
    from .obssetup import setup_obs
    return setup_obs()


class _FailedJob:
    """A report for a job that raised. Same shape as a real one, so the panel needs
    no special case for the failure it is most likely to see."""

    ok = False

    def __init__(self, lines: list[str]):
        self.lines = lines


@dataclass
class Studio:
    workers: Sequence[Worker]
    spawn: Callable[..., object] = _spawn
    terminate: Callable[[object], None] = kill_tree
    is_listening: Callable[[int], bool] = port_open
    reclaim: Callable[[int], bool] = reclaim_port
    port_owners: Callable[[int], list[int]] = port_owner_pids
    owner_command: Callable[[int], str] = process_command
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    on_status: Callable[..., None] = lambda **kw: None
    log_dir: str | None = None
    # How many consecutive missed port probes a LIVE process is allowed before it is
    # treated as dead. Not zero: a server busy inside one long operation can miss a
    # probe while being perfectly healthy, and a supervisor that kills on a single
    # miss turns that into a restart loop, which is the shape of the 2026-08-02 outage.
    probe_grace: int = 2
    max_consecutive_failures: int = 5
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    states: list[WorkerState] = field(default_factory=list)
    # The control dock runs on its own thread. Rather than lock the whole state machine,
    # it only ever READS status and posts requests here; the supervision loop drains
    # them between passes, so nothing mutates a worker from two threads at once. No
    # lock at all, on purpose: there used to be one, poll() held it through a whole
    # restart, and status() needed it, so the panel went blank for exactly as long as a
    # restart took, which is the one time anyone looks at it.
    _requests: deque = field(default_factory=deque, repr=False)
    _stop_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    # Building the OBS scenes: a one-shot on its own thread, not a worker. It dials
    # obs-websocket, which can take a connect timeout to fail, so neither the bring-up
    # nor the supervision loop waits on it; the panel reads `setup_state` the way it
    # reads worker status.
    obs_setup: Callable[[], object] = _default_obs_setup
    setup_state: dict = field(default_factory=lambda: {
        "running": False, "ok": None, "lines": [], "at": 0.0})
    #: Is OBS streaming? None until it answers. Kept here because the panel is the
    #: one surface an operator is looking at, and "am I actually live" is the
    #: question they most want answered without alt-tabbing to OBS.
    streaming: bool | None = None

    def __post_init__(self) -> None:
        if not self.states:
            self.states = [WorkerState(worker=w) for w in self.workers]

    def request_obs_setup(self, on_done: Callable[[object], None] | None = None) -> bool:
        """Build the OBS scenes on a thread. False if a run is already in flight."""
        return self._run_once(self.obs_setup, self.setup_state, "studio-obs-setup", on_done)

    def _run_once(self, job: Callable[[], object], state: dict, name: str,
                  on_done: Callable[[object], None] | None) -> bool:
        if state["running"]:
            return False
        state.update(running=True, ok=None, lines=[], at=time.time())

        def run() -> None:
            try:
                rep = job()
            except Exception as e:  # noqa: BLE001 - the thread must report, not die silently
                rep = _FailedJob([f"FAILED, {e}"])
            state.update(running=False, ok=bool(rep.ok), lines=list(rep.lines), at=time.time())
            if on_done is not None:
                on_done(rep)

        threading.Thread(target=run, daemon=True, name=name).start()
        return True

    # --- requests from the dock -------------------------------------------
    def request_restart(self, name: str) -> bool:
        """Queue a restart. False if there is no such worker, so the dock can say so."""
        if name not in {s.name for s in self.states}:
            return False
        self._requests.append(("restart", name))
        return True

    def request_stop(self) -> None:
        self._stop_requested.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def drain_requests(self) -> list[str]:
        """Apply queued requests. Called by the supervision loop, never by the dock."""
        done = []
        while self._requests:
            action, name = self._requests.popleft()
            st = next((s for s in self.states if s.name == name), None)
            if st is None or action != "restart":
                continue
            # An explicit restart clears the give-up state: the operator asking for it
            # IS the new information, and a worker you cannot retry from the dock is
            # exactly the one you end up restarting the whole studio for.
            st.failed = False
            st.consecutive_failures = 0
            st.next_retry_at = 0.0
            # Counted like any other restart, and that is a UI decision rather than a
            # bookkeeping one. The dock shows a status word and this number and no pid,
            # so a worker that was already healthy looks IDENTICAL after a manual
            # restart: the operator clicks, the process really does get replaced, and
            # nothing on screen moves. Counting it is the only feedback there is.
            st.restarts += 1
            if st.proc is not None and self._alive(st):
                try:
                    self.terminate(st.proc)
                except OSError:
                    pass
            self.on_status(worker=name, event="restart_requested")
            self._launch(st)
            done.append(name)
        return done

    # --- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Start every worker in order, waiting for each to serve. False if one never did.

        Deliberately NOT all-or-nothing: an overlay that will not come up should not
        stop the director from going to air, so a failed worker is recorded and the rest
        still start. The caller decides what a partial studio is worth.

        This is the ONE place that waits. Everything downstream dials the bridge, so the
        first bring-up has to be in order; a restart mid-race does not (see _launch and
        poll).
        """
        ok = True
        for st in self.states:
            if not self._launch(st) or (st.starting and not self._await_port(st)):
                ok = False
        return ok

    def _launch(self, st: WorkerState) -> bool:
        """Pre-flight the port, spawn, and mark the worker `starting`. Never waits.

        A worker with a port stays `starting` until poll() sees it answer or its
        ready_timeout runs out. start() waits for that, in order; poll() does not, so a
        slow restart mid-race neither blanks the panel nor blinds the supervisor to the
        other workers meanwhile.
        """
        port = st.worker.port
        st.starting, st.start_deadline = False, 0.0
        blame = None
        if port is not None and (st.proc is None or not self._alive(st)) \
                and self.is_listening(port):
            blame = self._reclaim_if_ours(st)
        if port is not None and self.is_listening(port):
            # Not marked failed: a port can still be held for a moment by the process we
            # just killed, so this has to stay retryable. The normal backoff gives up.
            st.last_error = blame or (
                f"port {port} is already in use - something else is serving it")
            self.on_status(worker=st.name, event="port_taken", port=port)
            return False
        try:
            st.proc = self.spawn(st.worker.argv, cwd=st.worker.cwd,
                                 log=self._log_path(st))
        except OSError as e:
            st.failed = True
            st.last_error = f"could not start: {e}"
            self.on_status(worker=st.name, event="spawn_failed", error=str(e))
            return False

        self.on_status(worker=st.name, event="started", pid=self._pid(st))
        if port is None:
            return True
        st.starting, st.start_deadline = True, self.clock() + st.worker.ready_timeout
        return True

    def _reclaim_if_ours(self, st: WorkerState) -> str | None:
        """Our process is gone and the port is still held. Take it back: from OUR orphan.

        The orphan is real and repeatedly observed: a venv's Scripts\\python.exe is a
        redirector that runs the real interpreter as a child, the child holds the
        socket, and killing the launcher leaves it there. Nothing will ever free it, so
        waiting cannot help (2026-08-02: 77 restarts of one worker in a single race).

        But "our process is gone" is also true of every worker on the very first start,
        and there the holder may be anything: a worker someone ran by hand in a
        terminal, a second studio, an unrelated server. So the holder is identified
        first, and only a command line that looks like one of ours is killed. Returns
        None when the port was reclaimed (or nothing held it), else the reason it was
        left alone, which becomes the worker's error.
        """
        port = st.worker.port
        holders = [(pid, self.owner_command(pid)) for pid in self.port_owners(port)]
        ours = [(pid, cmd) for pid, cmd in holders if self._looks_like_ours(st, cmd)]
        if ours:
            pid, cmd = ours[0]
            self.on_status(worker=st.name, event="reclaiming", port=port, pid=pid,
                           command=cmd[:160])
            if self.reclaim(port):
                self.on_status(worker=st.name, event="port_reclaimed", port=port)
                return None
            return f"port {port} is already in use by pid {pid} and it would not die"
        if holders:
            pid, cmd = holders[0]
            return (f"port {port} is already in use by pid {pid} "
                    f"({cmd[:80] or 'unknown command'}), which is not one of ours")
        return (f"port {port} is already in use by a process this box cannot identify; "
                f"not killing it")

    @staticmethod
    def _looks_like_ours(st: WorkerState, cmd: str) -> bool:
        """Would this command line have been spawned by a studio running this worker?"""
        markers = {"pylon"}
        markers |= {Path(str(a)).name for a in st.worker.argv if str(a).endswith(".py")}
        low = cmd.lower()
        return any(m.lower() in low for m in markers)

    def _await_port(self, st: WorkerState) -> bool:
        """Block until a `starting` worker serves, dies, or runs out of time.

        Used by start() only. Sets last_error itself, and the caller must not overwrite
        it: "exited during startup" and "never listened" call for completely different
        next moves, and flattening them into one message sends you looking for a
        binding problem in a process that is not running.
        """
        assert st.worker.port is not None
        while self.clock() < st.start_deadline:
            if self.is_listening(st.worker.port):
                self._became_ready(st)
                return True
            if not self._alive(st):
                st.last_error = "exited during startup"
                break
            self.sleep(0.25)
        else:
            st.last_error = (f"never listened on {st.worker.port} within "
                             f"{st.worker.ready_timeout:.0f}s")
        st.starting = False
        self.on_status(worker=st.name, event="not_ready", port=st.worker.port,
                       reason=st.last_error)
        return False

    def _became_ready(self, st: WorkerState) -> None:
        st.starting, st.start_deadline = False, 0.0
        st.consecutive_failures = 0
        st.probe_misses = 0
        self.on_status(worker=st.name, event="ready", port=st.worker.port)

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate every worker, youngest first.

        Reverse order because the bridge is what everything else is talking to: tearing
        it down first would have four workers log a flurry of connection errors on the
        way out, which buries whatever the real reason for stopping was.
        """
        for st in reversed(self.states):
            proc = st.proc
            if proc is None or not self._alive(st):
                continue
            try:
                self.terminate(proc)
            except OSError as e:
                st.last_error = f"terminate failed: {e}"
            self.on_status(worker=st.name, event="stopping")

        deadline = self.clock() + timeout
        for st in reversed(self.states):
            proc = st.proc
            while proc is not None and self._alive(st) and self.clock() < deadline:
                self.sleep(0.1)
            if proc is not None and self._alive(st):
                try:
                    proc.kill()  # type: ignore[attr-defined]
                    self.on_status(worker=st.name, event="killed")
                except OSError:
                    pass
            st.proc = None
            st.starting = False

    # --- supervision ------------------------------------------------------
    def poll(self) -> list[WorkerStatus]:
        """One supervision pass: restart what died, then report. Never waits.

        A worker that is alive but has stopped serving counts as dead. That is the case
        the PID check misses entirely, and it is the one that actually happens: a
        wedged server holding its process open while answering nothing.
        """
        now = self.clock()
        for st in self.states:
            if st.failed:
                continue
            if st.starting:
                if self._healthy(st):
                    self._became_ready(st)
                    continue
                alive = st.proc is not None and self._alive(st)
                if alive and now < st.start_deadline:
                    continue                       # still coming up; look elsewhere
                st.starting = False
                st.last_error = ("exited during startup" if not alive else
                                 f"never listened on {st.worker.port} within "
                                 f"{st.worker.ready_timeout:.0f}s")
                self.on_status(worker=st.name, event="not_ready", port=st.worker.port,
                               reason=st.last_error)
                # and fall through: this is a failure like any other, with backoff
            elif self._healthy(st):
                st.consecutive_failures = 0
                st.probe_misses = 0
                continue
            elif st.proc is not None and self._alive(st):
                # A worker whose PROCESS is gone is definitively dead. One that is
                # still alive but did not answer its port may simply be busy: a server
                # inside one long operation is unreachable and perfectly healthy.
                # Killing it for that is how a working worker becomes a restart loop,
                # so the probe has to miss repeatedly before it counts.
                st.probe_misses += 1
                if st.probe_misses <= self.probe_grace:
                    self.on_status(worker=st.name, event="probe_missed",
                                   misses=st.probe_misses, port=st.worker.port)
                    continue
            if now < st.next_retry_at:
                continue
            st.probe_misses = 0
            self._restart(st, now)
        return self.status()

    def _healthy(self, st: WorkerState) -> bool:
        if st.proc is None or not self._alive(st):
            return False
        if st.worker.port is None:
            return True
        return self.is_listening(st.worker.port)

    def _restart(self, st: WorkerState, now: float) -> None:
        st.consecutive_failures += 1
        if st.consecutive_failures > self.max_consecutive_failures:
            st.failed = True
            st.last_error = (f"gave up after {self.max_consecutive_failures} "
                             f"consecutive failures")
            self.on_status(worker=st.name, event="gave_up",
                           failures=st.consecutive_failures)
            return

        proc = st.proc
        if proc is not None and self._alive(st):
            # Alive but not serving: it has to go before its replacement can bind, and
            # it has to go as a TREE, or the redirector dies and the process holding
            # the port carries on without a parent.
            try:
                self.terminate(proc)
            except OSError:
                pass

        delay = min(self.backoff_base * (2 ** (st.consecutive_failures - 1)),
                    self.backoff_cap)
        st.next_retry_at = now + delay
        st.restarts += 1
        self.on_status(worker=st.name, event="restarting",
                       attempt=st.consecutive_failures, backoff=delay)
        self._launch(st)

    def status(self) -> list[WorkerStatus]:
        out = []
        for st in list(self.states):
            alive = st.proc is not None and self._alive(st)
            listening = None
            if st.worker.port is not None:
                listening = self.is_listening(st.worker.port) if alive else False
            out.append(WorkerStatus(name=st.name, pid=self._pid(st), alive=alive,
                                    listening=listening, restarts=st.restarts,
                                    failed=st.failed, last_error=st.last_error,
                                    starting=st.starting and alive))
        return out

    # --- process plumbing -------------------------------------------------
    def _alive(self, st: WorkerState) -> bool:
        proc = st.proc
        if proc is None:
            return False
        try:
            return proc.poll() is None  # type: ignore[attr-defined]
        except OSError:
            return False

    def _pid(self, st: WorkerState) -> int | None:
        return getattr(st.proc, "pid", None) if st.proc is not None else None

    def _log_path(self, st: WorkerState) -> str | None:
        return None if self.log_dir is None else str(Path(self.log_dir) / f"{st.name}.log")


def default_workers(*, python: str | None = None, bridge_port: int | None = None,
                    replay: str | None = None, replays: bool = False,
                    obs_scenes: bool = True, cwd: str | None = None,
                    settings: ShowSettings = SHOW) -> list[Worker]:
    """The standard set, in dependency order: bridge, overlay, director.

    Every port a worker binds is on its command line, taken from the same settings the
    studio probes, so the probe and the bind cannot drift apart and a worker's log
    opens with the numbers that matter. tests/test_cli.py parses each of these under
    the CLI itself. That is what catches a renamed flag before the studio does: a
    worker that rejects its own argv "exits during startup" five times and is given up
    on, with the reason in a log nobody opens.

    `replay` swaps the live sim for a recording, which is how this gets exercised
    without a session running.
    """
    py = python or sys.executable
    base = [py, "-m", "pylon"] if not getattr(sys, "frozen", False) else [py]
    port = settings.bridge_port if bridge_port is None else bridge_port
    ws = settings.bridge_url(port=port)
    bridge = [*base, "bridge", "--port", str(port)]
    bridge += ["--sdk", "--live"] if replay is None else ["--replay", replay]

    return [
        Worker("bridge", bridge, port=port, cwd=cwd),
        Worker("overlay", [*base, "overlay", "--bridge", ws,
                           "--http-port", str(settings.overlay_http_port),
                           "--ws-port", str(settings.overlay_ws_port)],
               port=settings.overlay_http_port, cwd=cwd),
        Worker("director", [*base, "live", ws,
                            *(["--obs-scenes"] if obs_scenes else []),
                            *(["--replays"] if replays else [])],
               port=None, cwd=cwd),  # dials out only; nothing to probe
    ]


def format_status(rows: Sequence[WorkerStatus]) -> str:
    """One line per worker, aligned, for a terminal or a log."""
    lines = []
    for r in rows:
        if r.failed:
            mark, note = "FAILED ", r.last_error
        elif not r.alive:
            mark, note = "down   ", r.last_error
        elif r.starting:
            mark, note = "start  ", "coming up"
        elif r.listening is False:
            mark, note = "no port", "alive but not serving"
        elif r.listening is None:
            mark, note = "running", "(no port to probe)"
        else:
            mark, note = "ok     ", ""
        pid = f"pid {r.pid}" if r.pid else "-"
        restarts = f" restarts {r.restarts}" if r.restarts else ""
        lines.append(f"  {r.name:<11} {mark} {pid:<10}{restarts}{'  ' + note if note else ''}")
    return "\n".join(lines)


def format_event(worker: str = "?", event: str = "?", **kw) -> str:
    """One supervision event as a log line: `[studio] overlay: restarted  attempt=2`."""
    extra = " ".join(f"{k}={v}" for k, v in kw.items() if v not in (None, ""))
    return f"[studio] {worker}: {event}" + (f"  {extra}" if extra else "")


def supervise(studio: Studio, *, interval: float, emit: Callable[[str], None],
              dock=None, sleep: Callable[[float], None] = time.sleep, onair=None) -> None:
    """Bring the workers up and keep them up until the panel asks to stop, or Ctrl-C.

    Prints only on CHANGE. A status line every few seconds is noise that trains you to
    ignore the one line that matters. `dock` is a running control server or None; it is
    shut down here so it cannot outlive the studio it reports on. `sleep` is injectable
    so the loop can be driven in a test without waiting. `onair` is a show.onair.OnAir
    or None; ticked every pass, it is what notices OBS starting to stream, which the
    panel shows as the LIVE lamp.
    """
    def fingerprint(rows):
        return tuple((r.name, r.ok, r.restarts, r.failed) for r in rows)

    try:
        if not studio.start():
            emit("[studio] not everything came up - see below")
        rows = studio.status()
        emit(format_status(rows))
        last = fingerprint(rows)
        while not studio.stop_requested:
            sleep(interval)
            for name in studio.drain_requests():
                emit(f"[studio] {name}: restart requested from the panel")
            rows = studio.poll()
            if fingerprint(rows) != last:
                emit(format_status(rows))
                last = fingerprint(rows)
            if onair is not None:
                if onair.tick():
                    emit("[studio] OBS has started streaming: you are live")
                studio.streaming = onair.streaming
        emit("[studio] stop requested from the panel")
    except KeyboardInterrupt:
        emit("\n[studio] stopping...")
    finally:
        if dock is not None:
            dock.shutdown()
        studio.stop()
        emit("[studio] all stopped")
