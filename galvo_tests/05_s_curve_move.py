#!/usr/bin/env python3
"""
Step 5 - Smooth S-Curve using PyVISA with real-time voltage stepping.
Stable version without *OPC? to avoid timeout.
"""

import sys
import time
import numpy as np
import pyvisa
from _common import resolve_resource

# ============================================================
# ★★★ USER CONFIGURABLE PARAMETERS ★★★
# ============================================================
STEPS = 40          # Reduce steps to give instrument more time per command
DURATION = 5.0      # Longer duration gives more time per step
# ============================================================


def move_s_curve_pyvisa(awg, x1, y1, x2, y2, duration,
                        steps=STEPS, s_factor=4.0, volt_per_deg=1.0,
                        first_call=False):
    """
    Move using S-curve by stepping DC voltage.
    No *OPC? to avoid timeout.
    """
    if duration <= 0:
        awg.write(f':SOUR1:VOLT:OFFS {x1 * volt_per_deg:.3f}')
        awg.write(f':SOUR2:VOLT:OFFS {y1 * volt_per_deg:.3f}')
        if first_call:
            awg.write(':OUTP1 ON')
            awg.write(':OUTP2 ON')
        return

    # ============================================================
    # Only initialize DC mode and outputs on the FIRST call
    # ============================================================
    if first_call:
        awg.write(':OUTP1 OFF')
        awg.write(':OUTP2 OFF')
        time.sleep(0.1)

        awg.write(':SOUR1:FUNC:SHAP DC')
        awg.write(':SOUR2:FUNC:SHAP DC')
        time.sleep(0.1)

        awg.write(':OUTP1 ON')
        awg.write(':OUTP2 ON')
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

    # Force last value exactly to target
    if len(x_volts) > 0:
        x_volts[-1] = vx2
        y_volts[-1] = vy2

    print(f"Moving ({x1:.1f},{y1:.1f}) -> ({x2:.1f},{y2:.1f}) over {duration}s "
          f"with {steps} steps (interval: {duration/steps*1000:.1f}ms)")

    # Set initial position
    awg.write(f':SOUR1:VOLT:OFFS {vx1:.3f}')
    awg.write(f':SOUR2:VOLT:OFFS {vy1:.3f}')
    time.sleep(0.1)

    # ============================================================
    # Step through the curve with small delay after each pair
    # ============================================================
    interval = duration / steps
    for i, (vx, vy) in enumerate(zip(x_volts, y_volts)):
        awg.write(f':SOUR1:VOLT:OFFS {vx:.3f}')
        awg.write(f':SOUR2:VOLT:OFFS {vy:.3f}')
        
        # Small delay to prevent buffer overflow
        time.sleep(0.002)   # 2ms after each pair
        
        # Wait for the remaining interval (minus the 2ms already spent)
        remaining = interval - 0.002
        if remaining > 0:
            time.sleep(remaining)
        else:
            time.sleep(0.001)

        if (i + 1) % 20 == 0 or i == 0:
            print(f"  Progress: {int((i+1)/steps*100)}%")

    # ============================================================
    # ★★★ NO *OPC? – just wait a bit and force final position ★★★
    # ============================================================
    print("Allowing instrument to finish processing...")
    time.sleep(0.5)   # Let the DG1022Z catch up

    # Force final position using :VOLT:OFFS (we are still in DC mode)
    print(f"Forcing final position to exactly ({x2:.1f}, {y2:.1f})...")
    awg.write(f':SOUR1:VOLT:OFFS {vx2:.3f}')
    awg.write(f':SOUR2:VOLT:OFFS {vy2:.3f}')
    time.sleep(0.2)

    # Send again to be absolutely sure
    awg.write(f':SOUR1:VOLT:OFFS {vx2:.3f}')
    awg.write(f':SOUR2:VOLT:OFFS {vy2:.3f}')
    time.sleep(0.2)

    print("Holding final position for 2 seconds...")
    time.sleep(2.0)

    print("Motion completed. Laser held at final position.")


def main():
    res = resolve_resource()
    if not res:
        print("ERROR: No galvo resource found.")
        return 1

    print(f"Connecting to {res} ...")
    rm = pyvisa.ResourceManager()
    awg = rm.open_resource(res)
    awg.timeout = 10000   # Increase timeout to 10 seconds

    try:
        idn = awg.query('*IDN?').strip()
        print(f"IDN: {idn}")
    except Exception as e:
        print(f"Communication error: {e}")
        awg.close()
        return 1

    try:
        # ============================================================
        # First move: (0,0) -> (3,0)
        # ============================================================
        move_s_curve_pyvisa(
            awg=awg,
            x1=0.0, y1=0.0,
            x2=3.0, y2=0.0,
            duration=DURATION,
            steps=STEPS,
            s_factor=4.0,
            volt_per_deg=1.0,
            first_call=True
        )

        time.sleep(1.0)

        # ============================================================
        # Second move: (3,0) -> (0,0)
        # ============================================================
        move_s_curve_pyvisa(
            awg=awg,
            x1=3.0, y1=0.0,
            x2=0.0, y2=0.0,
            duration=DURATION,
            steps=STEPS,
            s_factor=4.0,
            volt_per_deg=1.0,
            first_call=False
        )

        time.sleep(1.0)

        # ============================================================
        # Third move: (0,0) -> (0,-3)
        # ============================================================
        print("\nMoving down...")
        move_s_curve_pyvisa(
            awg=awg,
            x1=0.0, y1=0.0,
            x2=0.0, y2=-3.0,
            duration=DURATION,
            steps=STEPS,
            s_factor=4.0,
            volt_per_deg=1.0,
            first_call=False
        )

        time.sleep(1.0)

        # ============================================================
        # Fourth move: (0,-3) -> (0,0)
        # ============================================================
        print("\nMoving back to origin...")
        move_s_curve_pyvisa(
            awg=awg,
            x1=0.0, y1=-3.0,
            x2=0.0, y2=0.0,
            duration=DURATION,
            steps=STEPS,
            s_factor=4.0,
            volt_per_deg=1.0,
            first_call=False
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
