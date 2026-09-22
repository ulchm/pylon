"""The OBS control panel: the operator surface, served over the supervisor.

This is the whole reason no OBS plugin is needed. OBS can register any URL as a Custom
Browser Dock, so the panel lives inside the OBS window as a page (overlays/control.html)
served from here, talking to the process that already knows every worker's state. No
C++, no Qt, nothing to rebuild when OBS bumps its ABI.

For most people this panel IS Pylon: it is where the show is checked on, set up and
adjusted, and nothing it does requires a terminal.

    GET  /              the page
    GET  /api/status    every worker: alive, listening, restarts, failed, starting, error
    GET  /api/settings  the config file as JSON, plus where it lives
    POST /api/settings  save it (merged over the current one, atomically)
    POST /api/restart   {"worker": name}
    POST /api/obs-setup build the OBS scenes, on a thread
    POST /api/stop      ask the studio to end the broadcast (a request, not an action)

Loopback by default and deliberately so: these endpoints can restart workers, end the
broadcast and read a file holding a stream key, and there is no authentication because
OBS's browser dock cannot carry any. Keeping it off the LAN is what makes that
acceptable, and `/api/settings` never sends the stream key back out (see `_settings`).
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

from ..config import as_dict, config_path, from_dict
from ..config import load as load_config
from ..config import save as save_config
from ..settings import SHOW, reload_show
from .studio import Studio


class _ControlHandler(http.server.SimpleHTTPRequestHandler):
    """The dock page plus the small JSON API over the supervisor."""

    studio: Studio | None = None

    def log_message(self, fmt, *args):
        pass  # the supervision loop is the log; a line per poll would bury it

    def _json(self, obj, code: int = 200) -> None:
        try:
            self._write_json(obj, code)
        except OSError:
            # The dock is a browser page that polls: OBS hides it, the page navigates,
            # the socket goes. socketserver would print a ConnectionAbortedError
            # traceback per occurrence, and the studio console is 30 lines deep, so
            # that flood is what ERASES the one message explaining why a worker died.
            pass

    def _write_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/api/status":
            return self._json(self._snapshot())
        if route == "/api/settings":
            return self._json(self._settings())
        if self.path in ("", "/"):
            self.path = "/control.html"
        return super().do_GET()

    def _snapshot(self) -> dict:
        s = self.studio
        rows = s.status() if s is not None else []
        return {
            "stopping": bool(s is not None and s.stop_requested),
            "streaming": None if s is None else s.streaming,
            "setup": dict(s.setup_state) if s is not None else None,
            "workers": [{"name": r.name, "pid": r.pid, "alive": r.alive,
                         "listening": r.listening, "restarts": r.restarts,
                         "failed": r.failed, "ok": r.ok, "starting": r.starting,
                         "error": r.last_error}
                        for r in rows],
        }

    #: Values the panel is shown as "set, not telling you what it is". A browser
    #: source's page is trivially readable by anything that can reach this port, and a
    #: stream key is the one value here that lets a stranger broadcast as the operator.
    SECRETS = (("obs", "stream_key"), ("obs", "password"))
    REDACTED = "__kept__"

    def _settings(self) -> dict:
        """The config as the panel edits it, with the secrets masked."""
        cfg = load_config()
        doc = as_dict(cfg)
        for table, key in self.SECRETS:
            if doc.get(table, {}).get(key):
                doc[table][key] = self.REDACTED
        return {"path": str(config_path()), "settings": doc, "problems": cfg.problems}

    def _save_settings(self, patch: dict) -> dict:
        """Merge what the panel sent over what is on disk, and write it.

        A MERGE, not a replace: the panel may be an older build, or showing one tab,
        and a save must never blank a section it did not display. A masked secret sent
        back unchanged means "keep what is there", which is what lets the panel edit
        the rest of the form without ever holding the stream key.
        """
        current = as_dict(load_config())
        for table, values in (patch or {}).items():
            if not isinstance(values, dict) or table not in current:
                continue
            for key, value in values.items():
                if key not in current[table]:
                    continue
                if (table, key) in self.SECRETS and value == self.REDACTED:
                    continue
                current[table][key] = value
        cfg = from_dict(current)
        path = save_config(cfg)
        reload_show()
        return {"ok": True, "path": str(path), "problems": cfg.problems}

    def do_POST(self):
        # Settings first, and deliberately BEFORE the studio check: the config file is
        # this process's, not the supervisor's, and someone fixing a setting that
        # stopped the workers coming up must not be told there is no studio.
        if self.path == "/api/settings":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                patch = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "bad request body"}, 400)
            try:
                return self._json(self._save_settings(patch))
            except OSError as e:
                return self._json({"error": f"could not save: {e}"}, 500)

        s = self.studio
        if s is None:
            return self._json({"error": "no studio attached"}, 503)
        if self.path == "/api/stop":
            s.request_stop()
            return self._json({"ok": True, "stopping": True})
        if self.path == "/api/obs-setup":
            if not s.request_obs_setup():
                return self._json({"error": "a setup run is already going"}, 409)
            return self._json({"ok": True, "running": True}, 202)
        if self.path == "/api/restart":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                name = (json.loads(self.rfile.read(n) or b"{}") or {}).get("worker", "")
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "bad request body"}, 400)
            if not s.request_restart(name):
                return self._json({"error": f"no worker named {name!r}"}, 404)
            return self._json({"ok": True, "worker": name})
        return self._json({"error": "no such endpoint"}, 404)


def serve_control(studio: Studio, *, host: str = SHOW.loopback,
                  port: int = SHOW.control_port,
                  directory: Path = SHOW.overlays_dir):
    """Start the panel's server on a daemon thread and return it (see the module
    docstring for why the default host is loopback)."""
    class Handler(_ControlHandler):
        pass

    Handler.studio = studio

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        # SO_REUSEADDR means different things: on POSIX it lets us rebind past a
        # lingering TIME_WAIT, which we want; on Windows it lets a SECOND listener bind
        # a port that is actively served, so two studios would silently share the dock.
        allow_reuse_address = sys.platform != "win32"

    srv = Server((host, port), functools.partial(Handler, directory=str(directory)))
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="studio-control").start()
    return srv
