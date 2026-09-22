"""The frozen executable's entry point.

Two jobs that `pylon.cli:main` should not have to know about.

**Double-clicking has to do the obvious thing.** A frozen build launched from a
desktop shortcut gets no arguments, and someone who has just installed this expects
the broadcast to start, not a usage message. So bare `Pylon.exe` means `pylon studio`.
Every other invocation is the CLI exactly as documented, which is what keeps
`Pylon.exe doctor` and the studio's own worker spawns working.

**The workers are this same executable.** The studio spawns each worker by re-running
`sys.executable` with a subcommand (show/studio.default_workers), so the frozen build
must accept those subcommands and must NOT treat them as a double-click. It does not
have to do anything special for that: they arrive as arguments, and only the empty
case is special-cased.

**A crash must not close the window.** A frozen console application that raises during
startup prints a traceback and exits, and Windows closes the window with it, which is
the single most confusing way for a double-clicked launcher to behave: the person sees
a window flash and nothing else. So an unhandled exception is caught and held, but only
when this was double-clicked, because a WORKER that waits for a keypress is a worker
the studio waits out a ready timeout on and declares failed.
"""

from __future__ import annotations

import sys


def main() -> int:
    from pylon.cli import main as cli_main

    double_clicked = len(sys.argv) <= 1
    argv = ["studio"] if double_clicked else sys.argv[1:]
    try:
        return cli_main(argv)
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - the whole point is to show it and hold the window
        import traceback

        traceback.print_exc()
        if double_clicked:
            print("\nPylon stopped because of the error above.")
            print("Press Enter to close this window.")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
