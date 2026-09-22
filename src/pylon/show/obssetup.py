"""Build the OBS scenes from the config, as one callable job.

`pylon obs-setup` is the command form and the control panel's "Set up OBS" button is
the other, and neither should be the place the logic lives: the panel runs on a
thread inside the studio and cannot shell out, and the command has to report the same
thing the panel does. So both call this, and both get the same `SetupReport` back.

What it provisions, all idempotently, so running it twice is safe and running it again
after changing the config is the documented way to apply the change:

  * the programme scene: OBS's own game capture of the sim, the timing tower over it
  * three holding cards: starting soon, intermission, race complete
  * the stream destination, if the config names one (unset: OBS keeps what it has)
  * the encoder settings, which OBS reads at startup, so it says when to restart

The scenes are named from the show's tag, so two shows on one PC do not collide and
a scene list stays readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..config import load as load_config
from ..obs.setup import PROGRAM_SCENE
from ..settings import SHOW, StreamSettings


@dataclass
class SetupReport:
    """What happened, as lines the panel and the terminal both print.

    `ok` is False only when the scenes could not be built at all. A missing video
    source is NOT a failure: game capture reports nothing until it has hooked the sim,
    so a setup run made before the sim is up is both normal and correct, and saying
    "failed" there would send people hunting for a problem they do not have.
    """

    ok: bool = True
    lines: list[str] = field(default_factory=list)
    scenes: list[str] = field(default_factory=list)
    restart_obs: bool = False

    def say(self, line: str) -> None:
        self.lines.append(line)


def program_scene(cfg: Config) -> str:
    """The scene the race goes out on.

    Named after the show, exactly like the cards, so that two broadcasters on one PC
    cannot touch each other's scenes. That is not hypothetical: this product was cut
    from a broadcaster that still runs on its author's sim rig, and the first thing
    `obs-setup` would otherwise have done there is reach into the live show's
    programme scene.

    With no show name set at all it falls back to the plain name, which is what a
    single-broadcaster PC (the ordinary case) should see.
    """
    tag = (cfg.show.tag or cfg.show.name).strip()
    return f"{tag} - Broadcast" if tag else PROGRAM_SCENE


def scene_prefix(cfg: Config) -> str:
    """How the holding-card scenes are named.

    OBS sorts its scene list alphabetically, so a common prefix keeps the cards
    together and away from the programme scene. The show's tag is that prefix, which
    is what makes two shows on one PC readable; with no name set at all it falls back
    to the product's own, because scenes called " - Starting Soon" look broken.
    """
    tag = (cfg.show.tag or cfg.show.name).strip()
    return f"{tag} - " if tag else "Pylon - "


def source_prefix(cfg: Config) -> str:
    """How the SOURCES inside those scenes are named.

    Separate from `scene_prefix` because the fallbacks differ. OBS input names are
    unique across a whole scene collection, so two shows with properly separated
    scenes still collide on the sources inside them, and the second one to provision
    gets "a source already exists by that input name" and builds nothing. The tag
    fixes that. With no tag at all there is no second show to avoid, and the plain
    names are what every existing install already has in OBS, so it stays empty:
    renaming a working operator's sources to fix a collision they do not have would
    orphan the scene items pointing at them.
    """
    tag = (cfg.show.tag or cfg.show.name).strip()
    return f"{tag} - " if tag else ""


def setup_obs(cfg: Config | None = None, *, connect=None,
              game_window: str = "", capture_crop: str = "") -> SetupReport:
    """Provision OBS to broadcast this show. Never raises; the report says what happened."""
    from ..obs import (
        OutputSettings,
        build_program_scene,
        ensure_cards,
        ensure_output,
        ensure_stream_target,
    )
    from ..obs import connect as obs_connect

    cfg = cfg if cfg is not None else load_config()
    rep = SetupReport()
    dial = connect or obs_connect
    width, height = SHOW.canvas

    try:
        cl = dial(host=cfg.obs.host, port=cfg.obs.port,
                  password=cfg.obs.password or None)
    except Exception as e:  # noqa: BLE001 - one line about what to do, never a traceback
        rep.ok = False
        rep.say(f"Could not reach OBS at {cfg.obs.host}:{cfg.obs.port}.")
        rep.say("In OBS: Tools > WebSocket Server Settings > Enable WebSocket server.")
        rep.say(f"({e})")
        return rep

    sources = source_prefix(cfg)
    report = build_program_scene(cl, scene=program_scene(cfg),
                                 overlay_url=SHOW.overlay_page_url(),
                                 game_window=game_window, capture_crop=capture_crop,
                                 source_prefix=sources,
                                 width=width, height=height, fps=SHOW.fps)
    cards = ensure_cards(cl, base_url=SHOW.cards_url(), width=width, height=height,
                         prefix=scene_prefix(cfg), source_prefix=sources,
                         warnings=report["warnings"])
    rep.scenes = [report["scene"], *cards]
    rep.say(f"Scene '{report['scene']}' is ready at {width}x{height}.")
    rep.say(f"  video     {report['video'] or 'not created'}"
            f"{'' if report['video'] else ' (no capture source available)'}")
    rep.say(f"  overlay   {report['overlay'] or 'not created'}")
    rep.say(f"  cards     {', '.join(cards) or 'none'}")
    if not report["game"]:
        rep.say("  The video pane stays black until iRacing is running and this is "
                "run again; that is expected, not a failure.")

    stream = StreamSettings.from_config(cfg)
    if stream:
        st = ensure_stream_target(cl, server=stream.server, key=stream.key)
        rep.say(f"  stream    {st['server']} "
                f"({'set' if st['changed'] else 'already set'}, key "
                f"{'present' if st['key_set'] else 'MISSING'})")
        report["warnings"].extend(st["warnings"])
    else:
        rep.say("  stream    left as OBS has it")

    # The encoder is pinned ONLY when we are also setting the destination. A Custom
    # RTMP server gets none of the encoder rules OBS applies for a known service, so
    # provisioning one without the other ships a stream at whatever bitrate happened
    # to be set. But if the operator streams to their own channel through OBS's own
    # settings, those are theirs: rewriting them uninvited is presumptuous, and on a
    # PC that also runs another broadcaster it reaches outside our own scenes, since
    # encoder settings live in the OBS PROFILE and not in the scene collection.
    if stream:
        out = ensure_output(cl, OutputSettings())
        if out["changed"]:
            rep.restart_obs = True
            rep.say(f"  encoder   set ({', '.join(out['changed'])})")
        else:
            rep.say("  encoder   already set")
        report["warnings"].extend(out["warnings"])
    else:
        rep.say("  encoder   left as OBS has it")

    for w in report["warnings"]:
        rep.say(f"  ! {w}")
    if rep.restart_obs:
        rep.say("Restart OBS for the encoder settings to take effect.")
    return rep
