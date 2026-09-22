"""Drive OBS scene changes from the session state (the director's other actuator).

The director already decides what the *camera* looks at. This decides what the
*switcher* is showing, from the same world snapshot: a holding card before the
session gets going, the programme once it does, and a closing card once the
chequer has been out long enough.

Split in two on purpose:

  * `SceneDirector` is pure. Snapshot in, scene name out, no OBS, no clock of
    its own. That is the part with the editorial judgement in it, so it is the
    part worth testing.
  * `ObsSwitcher` is the actuator. It talks to obs-websocket and is written so
    that every possible failure is survivable: see its docstring.

Why `SessionState` and not the flags: it is the one channel that says where
the session is in its life rather than what is happening inside it, and it is
already on the snapshot. Session TYPE (`session_kind`) is on the snapshot too:
the bridge hands over the weekend's SessionInfo on connect and the kind is read
off each frame's SessionNum, so live it is known (the old sim-box agent never
forwarded it, issue #40, and that agent is retired). The one place the kind
changes the answer is GET_IN_CAR, below, and it is also the one place the
switcher looks past the session at the cars: the grid is live once it has
cars on it, which nothing session-level can say.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..telemetry.constants import SessionState
from ..world.model import SessionKind
from .setup import PROGRAM_SCENE, SCENE_PREFIX

# Where the session is in its life -> which scene belongs on air. INVALID is
# grouped with the pre-session states deliberately: it is what a feed reads
# before it has settled, and a holding card is the right thing to be showing
# when we do not yet know.
_PRE = (SessionState.INVALID, SessionState.GET_IN_CAR)
# WARMUP is the gridded pre-race, not a countdown: the cars are on the grid and
# the formation and green are seconds away. It lived in _PRE, so the Starting Soon
# card sat over the grid and the start and only cut to the programme at the
# formation lap, and on a short/standing start that landed AFTER green (operator,
# 2026-09-14: "we didn't see the grid / green flag part at all"). Live from warmup on.
#
# That was not enough. In a RACE session the grid is GET_IN_CAR, not WARMUP: the
# race session opens onto an empty grid, each driver puts their own car on it
# with the sim's Grid button when they are ready, the countdown runs at most two
# minutes, and WARMUP is the last few seconds before the lights. Round 1 (Watkins
# Glen, 2026-09-20, standing start): GET_IN_CAR 16:03:05, WARMUP 16:05:05, green
# 16:05:15: two minutes of forming grid under the Starting Soon card, and the
# operator cut to the programme by hand. Qualifying spent one second in
# GET_IN_CAR, practice none, so the state means "grid" only when the session is
# a race, and only once cars are on it: an ungridded car is not in the world
# (capture2 opens its race session with the pace car alone on pit road), so the
# card holds until `SceneConfig.grid_cars` cars read on_track, then the grid is
# on air as it fills. An UNKNOWN kind keeps the card: unlike the closing card,
# where "behave like a race" is the house rule, a holding card is the honest
# answer to "we do not know what this session is yet".
_LIVE = (SessionState.WARMUP, SessionState.PARADE_LAPS, SessionState.RACING)
_POST = (SessionState.CHECKERED, SessionState.COOL_DOWN)


@dataclass
class SceneConfig:
    """Scene names must match OBS exactly; `pylon obs-setup` creates these.

    Defaults come from the same constants obs-setup builds with, so the two
    agree by construction rather than by someone remembering to update both.
    """

    program: str = PROGRAM_SCENE
    pre: str = f"{SCENE_PREFIX}Starting Soon"
    post: str = f"{SCENE_PREFIX}Race Complete"
    # Practice and qualifying end under the same chequer a race does, and
    # "Race Complete" is a lie on both. Only reachable when session_kind is
    # actually known: live today it is not, so this is a fallback, not a path
    # to rely on.
    post_nonrace: str = f"{SCENE_PREFIX}Intermission"

    # In a race session's GET_IN_CAR, this many cars on the grid is a grid worth
    # showing. One is a driver who was quick on the button sitting alone on an
    # empty track; three reads as a field forming, and the rest arrive on air.
    # Editorial, not mechanical, so a small field or a different taste can move it.
    grid_cars: int = 3

    # A state has to hold this long before we cut to its scene. SessionState
    # flickers around session boundaries, and a switcher that chases every
    # flicker is worse than one that reacts a beat late.
    settle: float = 4.0
    # ...but the chequer is different. Cutting to a card the instant the leader
    # crosses the line throws away the finish: the cars still coming home, the
    # winner's slow-down lap. A race finishes one car at a time, and the sim says
    # when that is over: CHECKERED lasts until the last car has crossed (or its
    # timer runs out) and then the session enters COOL_DOWN. So the programme
    # stays up through the whole chequer and this long into the cool-down, then
    # the card. Round 1 (2026-09-20) cut to the card 64s after the flag with the
    # sim still 40s from cool-down and cars still finishing.
    post_race_hold: float = 30.0
    # The ceiling on the chequer phase, should the sim never reach cool-down (a
    # car crawling round with no timer): the card comes this long after the flag
    # regardless. Generous, because a finish is worth more than a card.
    chequer_max_hold: float = 240.0


class SceneDirector:
    """World snapshot -> which scene should be on air.

    `update` returns a scene name only when it CHANGES, so a caller can hand
    the result straight to a switcher without tracking the last value itself.
    Returns None to mean "nothing to do", including the important case of a
    feed that carries no SessionState at all: with no idea what the session is
    doing, the right move is to leave the producer's switcher alone.
    """

    def __init__(self, cfg: SceneConfig | None = None):
        self.cfg = cfg or SceneConfig()
        self.current: str | None = None
        self._pending: str | None = None      # candidate, not yet held long enough
        self._pending_since = 0.0
        self._chequer_at: float | None = None
        self._cooldown_at: float | None = None
        self._nonpost_since: float | None = None   # first frame out of CHECKERED
        self._last_time = 0.0

    def _closing_scene(self, snap) -> str:
        kind = snap.session.session_kind
        if kind in (SessionKind.PRACTICE, SessionKind.QUALIFY, SessionKind.WARMUP):
            return self.cfg.post_nonrace
        # UNKNOWN falls through to the race card, matching the house rule that
        # an unknown session behaves like a race rather than going silent.
        return self.cfg.post

    def want(self, snap) -> str | None:
        """The scene this snapshot argues for, before any settling."""
        state = snap.session.state
        if state is None:
            return None
        if state in _PRE:
            # The session is over and the sim has left it (INVALID): the closing
            # card stays, because "Starting Soon" over a race that just finished is
            # a lie. A real next session arrives as GET_IN_CAR and takes over.
            if (state == SessionState.INVALID
                    and self.current in (self.cfg.post, self.cfg.post_nonrace)):
                return self.current
            # the race session's GET_IN_CAR is the grid, live once cars are on it
            # (see _LIVE above)
            if (state == SessionState.GET_IN_CAR
                    and snap.session.session_kind == SessionKind.RACE
                    and self.gridded(snap) >= self.cfg.grid_cars):
                return self.cfg.program
            return self.cfg.pre
        if state in _LIVE:
            return self.cfg.program
        if state in _POST:
            # hold the programme through the finish before cutting away: the whole
            # chequer (the field is still crossing the line) and a little of the
            # cool-down (the winner's lap), then the card
            t = snap.session_time
            if self._chequer_at is None:
                return self.cfg.program
            done = (self._cooldown_at is not None
                    and t - self._cooldown_at >= self.cfg.post_race_hold)
            if done or t - self._chequer_at >= self.cfg.chequer_max_hold:
                return self._closing_scene(snap)
            return self.cfg.program
        return None

    @staticmethod
    def gridded(snap) -> int:
        """Cars on the grid. The builder keeps the pace car out of `cars` and a
        car that has not gridded is not in the world, so on_track is the count."""
        return sum(1 for c in snap.cars.values() if c.on_track)

    def update(self, snap) -> str | None:
        t = snap.session_time

        # Session time is not monotonic: a scrub back, a session rollover, a
        # reconnect onto a fresh model. Every timer here is derived from it, so
        # they all restart together rather than sitting in a future that will
        # not arrive for twenty minutes (the shape of issue #41).
        if t < self._last_time:
            self._pending = None
            self._chequer_at = None
            self._cooldown_at = None
            self._nonpost_since = None
        self._last_time = t

        if snap.session.state in _POST:
            self._nonpost_since = None
            if self._chequer_at is None:
                self._chequer_at = t
            if snap.session.state == SessionState.COOL_DOWN and self._cooldown_at is None:
                self._cooldown_at = t
        elif self._chequer_at is not None:
            # A frame or two out of CHECKERED is the same flicker the cut itself
            # settles against, and it used to restart the whole post-chequer hold.
            # Only a sustained return to something else lets the hold go.
            if self._nonpost_since is None:
                self._nonpost_since = t
            elif t - self._nonpost_since >= self.cfg.settle:
                self._chequer_at = None
                self._cooldown_at = None
                self._nonpost_since = None

        want = self.want(snap)
        if want is None or want == self.current:
            self._pending = None
            return None

        # settle: the candidate has to still be the candidate a few seconds later
        if want != self._pending:
            self._pending = want
            self._pending_since = t
            return None
        if t - self._pending_since < self.cfg.settle:
            return None

        self._pending = None
        self.current = want
        return want


class ObsSwitcher:
    """Applies a scene change to OBS, and never takes the director down with it.

    The director is the one worker a broadcast cannot afford to lose: if it
    stops, the sim camera freezes wherever it last pointed for the rest of the
    race. Scene switching is a nicety on top of that, so every failure here
    (OBS closed, websocket dropped, scene renamed, transition missing) is
    swallowed and reported, never raised. Worst case the producer switches by
    hand, which is what they were doing anyway.
    """

    def __init__(self, client, *, transition: str = "", on_event=None,
                 reconnect=None):
        self.cl = client
        self.transition = transition
        self.on_event = on_event
        # A factory for a fresh client. obs-websocket's ReqClient does not reconnect,
        # so after an OBS restart every cut failed for the rest of the show; with this
        # a failed cut is retried once on a new connection.
        self.reconnect = reconnect
        # None means "we could not read OBS", which is NOT the same as an
        # empty set meaning "OBS genuinely has none of these". Only the first
        # justifies going ahead and hoping.
        self._scenes: set[str] | None = None
        self._transitions: set[str] | None = None
        self._warned: set[str] = set()
        self.refresh()

    def _say(self, msg: str) -> None:
        if self.on_event is not None:
            self.on_event(msg)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self._say(msg)

    def refresh(self) -> None:
        """Re-read what OBS actually has, so a missing scene is a warning we can
        give at startup rather than a surprise at the chequer."""
        try:
            self._scenes = {s["sceneName"] for s in self.cl.get_scene_list().scenes}
            self._transitions = {t["transitionName"]
                                 for t in self.cl.get_scene_transition_list().transitions}
        except Exception as e:  # noqa: BLE001 - see class docstring
            self._say(f"could not read OBS scene list: {e}")

    def check(self, cfg: SceneConfig) -> list[str]:
        """Names in `cfg` that OBS does not have. Empty means good to go."""
        if self._scenes is None:
            return []
        wanted = {cfg.program, cfg.pre, cfg.post, cfg.post_nonrace}
        return sorted(w for w in wanted if w not in self._scenes)

    def apply(self, scene: str) -> bool:
        if self._scenes is not None and scene not in self._scenes:
            self._warn_once(f"scene:{scene}",
                            f"OBS has no scene named {scene!r} - skipping. "
                            f"Run `pylon obs-setup` to create the holding scenes.")
            return False
        try:
            # Set the transition first, so the cut we are about to make uses it.
            # A stinger has to be created by hand in OBS (obs-websocket cannot
            # make one), so a missing name here is expected, not broken.
            if self.transition:
                if (self._transitions is not None
                        and self.transition not in self._transitions):
                    self._warn_once(
                        f"trans:{self.transition}",
                        f"OBS has no transition named {self.transition!r} - using "
                        f"whatever is selected. See overlays/transitions/README.md.")
                else:
                    self.cl.set_current_scene_transition(self.transition)
            self.cl.set_current_program_scene(scene)
        except Exception as e:  # noqa: BLE001 - see class docstring
            self._say(f"OBS scene change to {scene!r} failed: {e}")
            if self.reconnect is None:
                return False
            try:
                self.cl = self.reconnect()
                self.refresh()
                self.cl.set_current_program_scene(scene)
            except Exception as e2:  # noqa: BLE001 - and the retry can fail too
                self._say(f"OBS reconnect failed: {e2}")
                return False
            self._say(f"OBS reconnected; scene -> {scene}")
            return True
        self._say(f"scene -> {scene}")
        return True
