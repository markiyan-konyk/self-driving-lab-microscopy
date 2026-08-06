"""
galvo_circle_test.py - Draw a circle with the galvos and see how fine it can get.

Precompute POINTS positions on a circle, then walk them, pushing each (x, y) as a DC
level to CH1/CH2 every PERIOD/POINTS seconds. Raise POINTS until the circle stops
filling in (the writes can't keep up) - that's the USBTMC/instrument ceiling.

Edit the constants below. Volts go straight into the galvo driver - keep RADIUS at spec.
"""

import time

import numpy as np

from DG1022Z import WaveGen

RESOURCE = None        # None = auto-pick the first USB VISA device, or hardcode "USB0::0x1AB1::0x0642::SERIAL::INSTR"
RADIUS   = 1.0         # circle radius, VOLTS
CENTER   = (0.0, 0.0)  # (x, y) center, volts
PERIOD   = 4.0         # seconds per full circle
POINTS   = 200         # points per circle -> update rate = POINTS / PERIOD
LAPS     = 10          # circles to draw

X_CH, Y_CH = 1, 2      # CH1 = X galvo, CH2 = Y galvo


def find_usb():
    import pyvisa
    for r in pyvisa.ResourceManager("@py").list_resources():
        if r.startswith("USB"):
            return r
    raise RuntimeError("no USB VISA instrument found")


theta = np.linspace(0, 2 * np.pi, POINTS, endpoint=False)
xs = CENTER[0] + RADIUS * np.cos(theta)
ys = CENTER[1] + RADIUS * np.sin(theta)
dt = PERIOD / POINTS

with WaveGen(RESOURCE or find_usb()) as gen:
    print("IDN:", gen.idn())
    for ch in (X_CH, Y_CH):
        gen.set_function(gen.DC, ch)
        gen.set_output_load(ch, gen.HIGH_Z)   # 50 ohm load would double the real output
        gen.output(True, ch)

    t0 = time.perf_counter()
    n = 0
    try:
        for _ in range(LAPS):
            for x, y in zip(xs, ys):
                gen.set_offset(x, X_CH)
                gen.set_offset(y, Y_CH)
                n += 1
                slack = t0 + n * dt - time.perf_counter()   # absolute schedule: no drift
                if slack > 0:
                    time.sleep(slack)
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.perf_counter() - t0
        for ch in (X_CH, Y_CH):
            gen.set_offset(0.0, ch)
        time.sleep(0.1)
        for ch in (X_CH, Y_CH):
            gen.output(False, ch)

    print(f"target {1/dt:.1f} pts/s   got {n/elapsed:.1f} pts/s   ({n} points in {elapsed:.1f}s)")
    print("errors:", gen.get_errors())
