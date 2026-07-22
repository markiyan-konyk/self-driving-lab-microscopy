#!/usr/bin/env python3
"""
Step 5 - Smooth S-Curve using PyVISA with reliable DC offset updates.
Debug version: prints each step's voltage.
"""

import sys
import time
import numpy as np
import pyvisa
from _common import resolve_resource


def move_s_curve_pyvisa(awg, x1, y1, x2, y2, duration,
                        steps=30, s_factor=4.0, volt_per_deg=1.0):
    """
    Move using S-curve by updating :VOLT:OFFS in DC mode.
    One-time DC mode setup, then only offset changes.
    """
    if duration <= 0:
        awg.write(f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        awg.write(f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        awg.write(':OUTP1 ON')
        awg.write(':OUTP2 ON')
        return

    # Turn off outputs to reset state
    awg.write(':OUTP1 OFF')
    awg.write(':OUTP2 OFF')
    time.sleep(0.2)

    # Set both channels to DC mode (do this only once!)
    awg.write(':SOUR1:FUNC:SHAP DC')
    awg.write(':SOUR2:FUNC:SHAP DC')
    time.sleep(0.2)

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
          f"with {steps} steps")
    print(f"Voltage range: X = {min(x_volts):.3f}V ~ {max(x_volts):.3f}V, "
          f"Y = {min(y_volts):.3f}V ~ {max(y_volts):.3f}V")

    # Set initial position using :VOLT:OFFS (mode already DC)
    awg.write(f':SOUR1:VOLT:OFFS {vx1:.3f}')
    awg.write(f':SOUR2:VOLT:OFFS {vy1:.3f}')
    awg.write(':OUTP1 ON')
    awg.write(':OUTP2 ON')
    time.sleep(0.3)  # Give time to settle

    # Step through the curve
    interval = duration / steps
    for i, (vx, vy) in enumerate(zip(x_volts, y_volts)):
        # Send both commands
        awg.write(f':SOUR1:VOLT:OFFS {vx:.3f}')
        awg.write(f':SOUR2:VOLT:OFFS {vy:.3f}')
        # Wait for the step interval
        time.sleep(interval)

        # Print progress every 5 steps
        if (i + 1) % 5 == 0 or i == 0:
            print(f"  Step {i+1}/{steps}: X={vx:.3f}V, Y={vy:.3f}V")

    # Hold final position for 3 seconds
    print("Holding final position for 3 seconds...")
    time.sleep(3.0)

    print("Motion completed. Laser held at final position.")


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
        # Move from (0,0) to (5,5) in 5 seconds
        move_s_curve_pyvisa(
            awg=awg,
            x1=0.0, y1=0.0,
            x2=5.0, y2=5.0,
            duration=5.0,
            steps=30,           # 30 steps -> ~0.167s per step
            s_factor=4.0,
            volt_per_deg=1.0
        )

        time.sleep(1.0)

        # Move back to origin
        print("\nMoving back to origin...")
        move_s_curve_pyvisa(
            awg=awg,
            x1=5.0, y1=5.0,
            x2=0.0, y2=0.0,
            duration=5.0,
            steps=30,
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
