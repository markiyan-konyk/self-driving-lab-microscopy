#!/usr/bin/env python3
"""
Step 5 - Smooth S-Curve using PyVISA with real-time voltage stepping.
Fixed: No delay between X and Y commands, no output toggling between moves.
"""

import sys
import time
import numpy as np
import pyvisa
from _common import resolve_resource

# ============================================================
# ★★★ USER CONFIGURABLE PARAMETERS ★★★
# ============================================================
STEPS = 80          # Number of steps (higher = smoother)
DURATION = 5.0      # Total movement time in seconds
# ============================================================


def move_s_curve_pyvisa(awg, x1, y1, x2, y2, duration,
                        steps=STEPS, s_factor=4.0, volt_per_deg=1.0,
                        first_call=False):
    """
    Move using S-curve by stepping DC voltage in real-time.
    No delay between X and Y commands for perfect diagonal motion.
    
    Args:
        first_call: If True, initialize DC mode and turn on outputs.
                    If False, assume outputs are already ON and in DC mode.
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
        # Turn off outputs to reset state (only once)
        awg.write(':OUTP1 OFF')
        awg.write(':OUTP2 OFF')
        time.sleep(0.1)

        # Set both channels to DC mode
        awg.write(':SOUR1:FUNC:SHAP DC')
        awg.write(':SOUR2:FUNC:SHAP DC')
        time.sleep(0.1)

        # Enable outputs
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

    # Force last value to be EXACTLY the target (avoid floating-point errors)
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
    # ★★★ CRITICAL FIX: No delay between X and Y commands ★★★
    # Send both commands back-to-back, then sleep for the interval.
    # ============================================================
    interval = duration / steps
    for i, (vx, vy) in enumerate(zip(x_volts, y_volts)):
        # Send X and Y commands IMMEDIATELY after each other
        awg.write(f':SOUR1:VOLT:OFFS {vx:.3f}')
        awg.write(f':SOUR2:VOLT:OFFS {vy:.3f}')
        
        # Now wait for the step interval
        time.sleep(interval)

        # Print progress every 20 steps
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  Progress: {int((i+1)/steps*100)}%")

    # ============================================================
    # ★★★ Force final position using :VOLT:OFFS (not :APPL:DC) ★★★
    # We're already in DC mode, so just update the offset.
    # ============================================================
    print(f"Forcing final position to exactly ({x2:.1f}, {y2:.1f})...")
    time.sleep(0.3)

    # Send final position twice for reliability
    awg.write(f':SOUR1:VOLT:OFFS {vx2:.3f}')
    awg.write(f':SOUR2:VOLT:OFFS {vy2:.3f}')
    time.sleep(0.2)
    awg.write(f':SOUR1:VOLT:OFFS {vx2:.3f}')
    awg.write(f':SOUR2:VOLT:OFFS {vy2:.3f}')
    time.sleep(0.2)

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
        # ============================================================
        # First move: (0,0) -> (5,5)  (first_call=True initializes DC mode)
        # ============================================================
        move_s_curve_pyvisa(
            awg=awg,
            x1=0.0, y1=0.0,
            x2=5.0, y2=5.0,
            duration=DURATION,
            steps=STEPS,
            s_factor=4.0,
            volt_per_deg=1.0,
            first_call=True      # ← Only the first call initializes
        )

        time.sleep(1.0)

        # ============================================================
        # Second move: (5,5) -> (0,0)  (first_call=False, no output toggling)
        # ============================================================
        print("\nMoving back to origin...")
        move_s_curve_pyvisa(
            awg=awg,
            x1=5.0, y1=5.0,
            x2=0.0, y2=0.0,
            duration=DURATION,
            steps=STEPS,
            s_factor=4.0,
            volt_per_deg=1.0,
            first_call=False     # ← Output stays ON, no mode reset
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
