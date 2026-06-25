"""Step 4 - Prolonged motion from ONE command (the wave-gen does the work).

Instead of the Pi streaming a stream of points, we tell the DG1022Z to
*generate* a waveform on each axis -> the laser traces a shape autonomously.
This proves the "one long command" pattern that a Phase-2 ROS action will wrap
for optical-trap functions: the host issues a single waveform program and the
instrument runs it on its own.

Pick a pattern:

    python galvo_tests/04_waveform.py            # circle (default)
    python galvo_tests/04_waveform.py circle     # X=cos, Y=sin (90 deg apart)
    python galvo_tests/04_waveform.py sine        # X=ramp, Y=sine -> sine curve
    python galvo_tests/04_waveform.py lissajous  # different X/Y freqs

WHY A SINGLE SINE LOOKS LIKE A LINE: a galvo points the mirror proportional to
its input volts, so one sine = the spot oscillating back and forth in time.
Two equal *in-phase* sines give X=Y -> a diagonal line. You need a 90 deg phase
offset between the axes for a circle, or a ramp on one axis to draw a sine curve.

SAFETY: amplitudes stay small and are clamped in galvo.py so the instantaneous
voltage can never exceed the galvo's +/-15 V limit. Ctrl-C stops; the channels
are returned to DC 0 and turned off on exit.
"""

import sys
import time

from _common import resolve_resource

from galvo import Galvo  # noqa: E402


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "circle").lower()
    if mode not in ("circle", "sine", "lissajous"):
        print(f"Unknown mode {mode!r}. Use: circle | sine | lissajous")
        return 2

    res = resolve_resource()
    if not res:
        print("No galvo resource. Set GALVO_RESOURCE or plug in the AWG.")
        return 1

    amp_vpp = 1.0    # volts peak-to-peak; keep small for untested mirrors
    run_s = 8.0

    g = Galvo(res)
    try:
        if mode == "circle":
            freq = 2.0
            print(f"Circle: X=cos / Y=sin at {freq} Hz, {amp_vpp} Vpp, {run_s:.0f}s")
            g.apply_sine(1, freq, amp_vpp, offset_v=0.0, phase_deg=0.0)
            g.apply_sine(2, freq, amp_vpp, offset_v=0.0, phase_deg=90.0)
            g.sync_phase()  # without this the 90 deg offset won't hold -> a line

        elif mode == "sine":
            x_freq = 1.0   # ramp sweeps the "horizontal" axis once per period
            y_freq = 8.0   # sine on the vertical axis -> wiggles drawn along X
            print(f"Sine curve: X=ramp {x_freq} Hz, Y=sine {y_freq} Hz, "
                  f"{amp_vpp} Vpp, {run_s:.0f}s")
            g.apply_ramp(1, x_freq, amp_vpp, offset_v=0.0, symmetry_pct=100.0)
            g.apply_sine(2, y_freq, amp_vpp, offset_v=0.0)

        else:  # lissajous
            x_freq, y_freq = 2.0, 3.0
            print(f"Lissajous: X sine {x_freq} Hz, Y sine {y_freq} Hz, "
                  f"{amp_vpp} Vpp, {run_s:.0f}s")
            g.apply_sine(1, x_freq, amp_vpp, offset_v=0.0)
            g.apply_sine(2, y_freq, amp_vpp, offset_v=0.0)

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
