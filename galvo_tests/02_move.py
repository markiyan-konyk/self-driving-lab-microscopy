"""Step 2 - Do the mirrors actually move?

Sweeps each galvo axis through a small voltage range while you watch the laser
spot on the camera feed. Confirms (a) that the mirrors deflect at all and
(b) which screen direction each axis and sign maps to -- note this down, the UI
laser-jog mapping in client.py uses the same convention and may need its signs
flipped to match what you see here.

SAFETY: starts and ends at 0 V, steps gently, and stays well inside
galvo.V_MIN..V_MAX. Ctrl-C stops; outputs are turned off on exit.

    python galvo_tests/02_move.py
"""

import sys
import time

from _common import resolve_resource

from DG1022Z import Galvo, V_MAX  # noqa: E402  (path set up in _common)


def main():
    res = resolve_resource()
    if not res:
        print("No galvo resource. Set GALVO_RESOURCE or plug in the AWG.")
        return 1

    g = Galvo(res)
    amp = min(1.0, V_MAX)   # keep the very first motion test gentle
    steps = 21
    try:
        for axis in ("x", "y"):
            print(f"\nSweeping {axis.upper()} from {-amp:+.2f} V to {amp:+.2f} V "
                  f"-- watch which way the spot goes.")
            for i in range(steps):
                v = -amp + 2 * amp * i / (steps - 1)
                g.set_volts(v, 0.0) if axis == "x" else g.set_volts(0.0, v)
                print(f"  {axis} = {v:+.3f} V")
                time.sleep(0.2)
            g.set_volts(0.0, 0.0)
            time.sleep(0.5)
        print("\nDone. If the spot moved on both axes, Step 2 passes.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1
    finally:
        g.set_volts(0.0, 0.0)
        g.off()
        g.close()


if __name__ == "__main__":
    sys.exit(main())
