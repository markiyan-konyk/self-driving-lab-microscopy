"""Step 4 - Prolonged motion from ONE command (the wave-gen does the work).

Instead of the Pi streaming a stream of points, we tell the DG1022Z to
*generate* a sine on each axis -> the laser traces a circle (or a Lissajous if
the X/Y frequencies differ). This proves the "one long command" pattern that a
Phase-2 ROS action will wrap for optical-trap functions: the host issues a
single waveform program and the instrument runs it autonomously.

    python galvo_tests/04_waveform.py

SAFETY: amplitudes stay small. Ctrl-C stops; the channels are returned to DC 0
and turned off on exit.
"""

import sys
import time

from _common import resolve_resource

from galvo import Galvo  # noqa: E402


def main():
    res = resolve_resource()
    if not res:
        print("No galvo resource. Set GALVO_RESOURCE or plug in the AWG.")
        return 1

    freq_x = 2.0     # Hz
    freq_y = 2.0     # Hz  (set != freq_x for a Lissajous figure)
    amp_vpp = 1.0    # volts peak-to-peak; keep small for untested mirrors
    run_s = 8.0

    g = Galvo(res)
    try:
        print(f"Driving a circle: X sine {freq_x} Hz, Y sine {freq_y} Hz, "
              f"{amp_vpp} Vpp, for {run_s:.0f}s ...")
        g.apply_sine(1, freq_x, amp_vpp, offset_v=0.0)
        g.apply_sine(2, freq_y, amp_vpp, offset_v=0.0)
        # The instrument now runs autonomously -- the Pi just waits.
        time.sleep(run_s)
        print("Done. Returning to DC pointing at 0 V.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1
    finally:
        g.point_mode()
        g.set_volts(0.0, 0.0)
        g.off()
        g.close()


if __name__ == "__main__":
    sys.exit(main())
