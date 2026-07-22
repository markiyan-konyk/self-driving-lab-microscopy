#!/usr/bin/env python3
"""
Step 5 - Smooth S-Curve using PyVISA with real-time voltage stepping.

This uses short DC commands (no large data transfer) to avoid USB timeouts.
The laser moves smoothly over the specified duration.
"""

import sys
import time
import numpy as np
import pyvisa
from _common import resolve_resource


def move_s_curve_pyvisa(awg, x1, y1, x2, y2, duration,
                        steps=100, s_factor=4.0, volt_per_deg=1.0):
    """
    Move using S-curve by stepping DC voltage in real-time.
    No large data transfers -> no USB timeout.
    """
    if duration <= 0:
        awg.write(f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        awg.write(f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        awg.write(':OUTP1 ON')
        awg.write(':OUTP2 ON')
        return

    # Turn off outputs before starting
    awg.write(':OUTP1 OFF')
    awg.write(':OUTP2 OFF')
    time.sleep(0.1)

    # Generate S-curve weights (0 to 1)
    t = np.linspace(-s_factor, s_factor, steps)
    weights = (np.tanh(t) + 1.0) / 2.0

    # Compute voltage arrays
    vx1 = x1 * volt_per_deg
    vx2 = x2 * volt_per_deg
    vy1 = y1 * volt_per_deg
    vy2 = y2 * volt_per_deg

    x_volts = vx1 + (vx2 - vx1) * weights
    y_volts = vy1 + (vy2 - vy1) * weights

    print(f"Moving ({x1:.1f},{y1:.1f}) -> ({x2:.1f},{y2:.1f}) over {duration}s "
          f"with {steps} steps...")

    # Set amplitude and offset to 0 (we'll use DC offset directly)
    awg.write(':SOUR1:VOLT 0')
    awg.write(':SOUR1:VOLT:OFFS 0')
    awg.write(':SOUR2:VOLT 0')
    awg.write(':SOUR2:VOLT:OFFS 0')
    awg.write(':OUTP1 ON')
    awg.write(':OUTP2 ON')
    time.sleep(0.1)

    # Step through the voltage curve
    interval = duration / steps
    for vx, vy in zip(x_volts, y_volts):
        awg.write(f':SOUR1:VOLT:OFFS {vx:.3f}')
        awg.write(f':SOUR2:VOLT:OFFS {vy:.3f}')
        time.sleep(interval)

    # Hold final position for 3 seconds (visible confirmation)
    print("Holding final position for 3 seconds...")
    time.sleep(3.0)

    # Optionally turn off outputs (or leave them on)
    awg.write(':OUTP1 OFF')
    awg.write(':OUTP2 OFF')
    print("Motion completed.")


def main():
    res = resolve_resource()
    if not res:
        print("ERROR: No galvo resource found.")
        return 1

    print(f"Connecting to {res} ...")
    rm = pyvisa.ResourceManager()
    awg = rm.open_resource(res)
    awg.timeout = 5000

    try:
        idn = awg.query('*IDN?').strip()
        print(f"IDN: {idn}")
    except Exception as e:
        print(f"Communication error: {e}")
        awg.close()
        return 1

    try:
        # Move from (0,0) to (10,10) in 5 seconds with 100 steps
        move_s_curve_pyvisa(
            awg=awg,
            x1=0.0, y1=0.0,
            x2=5.0, y2=5.0,
            duration=5.0,
            steps=100,          # 100 steps = 50ms per step -> smooth
            s_factor=4.0,
            volt_per_deg=1.0
        )

        # Wait and move back
        time.sleep(1.0)
        print("\nMoving back to origin...")
        move_s_curve_pyvisa(
            awg=awg,
            x1=10.0, y1=10.0,
            x2=0.0, y2=0.0,
            duration=5.0,
            steps=100,
            s_factor=4.0,
            volt_per_deg=1.0
        )

    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Shutting down outputs...")
        try:
            awg.write(':OUTP1 OFF')
            awg.write(':OUTP2 OFF')
        except:
            pass
        awg.close()
        print("Done.")


if __name__ == "__main__":
    sys.exit(main())
