"""Step 3 - How precisely does it move? Produce the calibration numbers.

Commands the galvo to a set of known voltage probe points and, for each, asks
you to read back where the laser spot landed in the camera image (in native
640x480 pixels -- use the UI's Measure tool or the crosshair to read a pixel
coordinate). It then least-squares fits the linear map

    [px_x, px_y] = PIXELS_PER_VOLT @ [vx, vy] + centre

and prints the constants to paste into microscope/tweezer.py. The residual it
reports is your precision / linearity check.

    python galvo_tests/03_precision.py

This one is interactive (it waits for you to type pixel coordinates).
"""

import sys
import time

import numpy as np

from _common import resolve_resource

from DG1022Z import Galvo, V_MAX  # noqa: E402


def _ask_pixel(prompt):
    """Read an 'x y' (or 'x,y') pixel coordinate from stdin; blank to skip."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        parts = raw.replace(",", " ").split()
        try:
            return float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            print("  Please type two numbers, e.g. '318 244' (or blank to skip).")


def main():
    res = resolve_resource()
    if not res:
        print("No galvo resource. Set GALVO_RESOURCE or plug in the AWG.")
        return 1

    dv = min(0.5, V_MAX)  # probe amplitude; keep modest so motion stays linear
    probes = [(0.0, 0.0), (dv, 0.0), (-dv, 0.0), (0.0, dv), (0.0, -dv)]

    g = Galvo(res)
    samples = []  # (vx, vy, px_x, px_y)
    try:
        print("\nFor each probe voltage, read the laser spot's pixel position "
              "from the UI and type it in.\n")
        for vx, vy in probes:
            g.set_volts(vx, vy)
            time.sleep(0.4)
            px = _ask_pixel(f"  v=({vx:+.3f}, {vy:+.3f}) V -> spot pixel 'x y': ")
            if px is not None:
                samples.append((vx, vy, px[0], px[1]))
    finally:
        g.set_volts(0.0, 0.0)
        g.off()
        g.close()

    if len(samples) < 3:
        print("\nNeed at least 3 measured points to fit. Got "
              f"{len(samples)}. Re-run.")
        return 1

    arr = np.array(samples, dtype=float)
    V = arr[:, :2]
    P = arr[:, 2:]
    # Design matrix [vx, vy, 1]; solve for [a b cx; c d cy] (px = A v + centre).
    A_design = np.hstack([V, np.ones((len(V), 1))])
    coef, *_ = np.linalg.lstsq(A_design, P, rcond=None)  # 3x2
    ppv = coef[:2, :].T           # 2x2 pixels-per-volt
    centre = coef[2, :]           # 2 image centre
    resid = P - A_design @ coef
    rms = float(np.sqrt(np.mean(resid ** 2)))

    print("\n=== Calibration result (paste into microscope/tweezer.py) ===")
    print(f"PIXELS_PER_VOLT = (({ppv[0, 0]:+.3f}, {ppv[0, 1]:+.3f}),")
    print(f"                   ({ppv[1, 0]:+.3f}, {ppv[1, 1]:+.3f}))")
    print(f"IMAGE_CENTRE_PX = {{\"x\": {centre[0]:.1f}, \"y\": {centre[1]:.1f}}}")
    print(f"\nfit RMS residual: {rms:.2f} px  (lower = more precise/linear)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
