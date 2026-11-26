#!/usr/bin/env python3
"""conductor.py -- entry point of the plug-array tank controller.

    python conductor.py tick | loop | plan | verify-countdown | health |
                        onboard | switch | measure-lamp | scan

Every command is handled by tank_tick.py (the minute planner, actuator and
observer for the plug array; design in data/docs/pi-contingency-plan.md
section 3.6). This file exists so the command name used in the docs, compose
and the alerts stays `conductor.py`.
"""
import sys

# Must equal tank_tick.COMMANDS (a test checks).
TANK_COMMANDS = ("tick", "loop", "plan", "verify-countdown", "health",
                 "onboard", "switch", "measure-lamp", "scan")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] not in TANK_COMMANDS:
        print("usage: conductor.py " + " | ".join(TANK_COMMANDS), file=sys.stderr)
        return 2
    import tank_tick
    return tank_tick.main(argv)


if __name__ == "__main__":
    sys.exit(main())
