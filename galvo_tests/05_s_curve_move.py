"""
Step 5 - Smooth S-Curve point-to-point movement using Arbitrary Waveform.

This script moves the laser from point A to point B over N seconds using a
tanh S-curve profile. This minimizes mechanical jerk and ensures the smoothest
possible motion for the galvo mirrors.

Usage:
    python galvo_tests/05_s_curve_move.py
"""

import sys
import time
import numpy as np
import pyvisa
from _common import resolve_resource


def move_s_curve_direct(awg, x1, y1, x2, y2, duration_sec,
                        sample_rate_hz=500, s_factor=4.0, volt_per_deg=1.0):
    """
    Move the galvo from (x1, y1) to (x2, y2) using a tanh S-curve profile.

    This function uploads a custom arbitrary waveform to the DG1000Z to
    generate a smooth, vibration-free motion.

    Parameters:
        awg (pyvisa.Resource): The open VISA resource of the DG1000Z.
        x1, y1 (float): Starting coordinates in degrees.
        x2, y2 (float): Target coordinates in degrees.
        duration_sec (float): Total travel time in seconds.
        sample_rate_hz (int): Output update rate (Hz). Default 500 (safe for USB).
        s_factor (float): Steepness of the S-curve. 4.0 is optimal.
        volt_per_deg (float): Galvo scaling factor (1.0, 0.8, or 0.5).
    """
    if duration_sec <= 0:
        # Instant jump using DC mode
        awg.write(f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        awg.write(f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        awg.write(':OUTP1 ON')
        awg.write(':OUTP2 ON')
        return

    # 1. Calculate total points (DG1000Z memory limit is 16384)
    total_points = int(duration_sec * sample_rate_hz)
    if total_points > 16384:
        sample_rate_hz = 16384 / duration_sec
        total_points = 16384
        print(f"Warning: Points exceeded limit. Adjusted sample rate to {sample_rate_hz:.1f} Hz")
    elif total_points < 10:
        total_points = 10
        sample_rate_hz = total_points / duration_sec

    print(f"Total waveform points: {total_points} (sample rate: {sample_rate_hz:.0f} Hz)")

    # 2. Generate tanh S-curve weights (ranging from 0 to 1)
    t = np.linspace(-s_factor, s_factor, total_points)
    normalized_pos = (np.tanh(t) + 1.0) / 2.0

    # 3. Convert degrees to voltages and interpolate
    vx_start = x1 * volt_per_deg
    vx_end = x2 * volt_per_deg
    vy_start = y1 * volt_per_deg
    vy_end = y2 * volt_per_deg

    x_volts = (vx_start + (vx_end - vx_start) * normalized_pos)
    y_volts = (vy_start + (vy_end - vy_start) * normalized_pos)

    # 4. Convert voltages to DG1000Z DAC integers (0 ~ 16383 for -10V ~ +10V)
    def volts_to_dac(voltage_array):
        return np.clip(((voltage_array + 10.0) / 20.0) * 16383, 0, 16383).astype(int)

    x_dac = volts_to_dac(x_volts)
    y_dac = volts_to_dac(y_volts)

    # 5. Upload and play waveforms on both channels
    print(f"Moving S-curve: ({x1:.2f}, {y1:.2f}) -> ({x2:.2f}, {y2:.2f}) in {duration_sec}s...")

    # Channel 1 (X-axis)
    awg.write(':SOUR1:FUNC:SHAP ARB')
    awg.write(f':SOUR1:FUNC:ARB:SRATE {sample_rate_hz:.0f}')
    awg.write(f':SOUR1:TRACE:DATA VOLATILE,{",".join(map(str, x_dac))}')
    awg.write('*OPC?')  # Wait for the operation to complete
    awg.read()          # Read the '1' response

    # Channel 2 (Y-axis)
    awg.write(':SOUR2:FUNC:SHAP ARB')
    awg.write(f':SOUR2:FUNC:ARB:SRATE {sample_rate_hz:.0f}')
    awg.write(f':SOUR2:TRACE:DATA VOLATILE,{",".join(map(str, y_dac))}')
    awg.write('*OPC?')
    awg.read()

    # Set amplitude and offset (full scale ±10V)
    awg.write(':SOUR1:VOLT 20')
    awg.write(':SOUR1:VOLT:OFFS 0')
    awg.write(':OUTP1 ON')

    awg.write(':SOUR2:VOLT 20')
    awg.write(':SOUR2:VOLT:OFFS 0')
    awg.write(':OUTP2 ON')

    # 6. Wait for the motion to complete
    time.sleep(duration_sec + 0.05)

    # 7. Stop the loop and hold the final position (DC mode)
    awg.write(':SOUR1:FUNC:SHAP DC')
    awg.write(f':SOUR1:VOLT:OFFS {vx_end}')
    awg.write(':SOUR2:FUNC:SHAP DC')
    awg.write(f':SOUR2:VOLT:OFFS {vy_end}')
    print("Motion completed. Laser held at target position.")


def main():
    # Automatically find the DG1000Z using the same logic as 01_connect.py
    res = resolve_resource()
    if not res:
        print("ERROR: No galvo resource found. Plug in the DG1022Z or set GALVO_RESOURCE.")
        return 1

    print(f"Connecting to {res} ...")
    rm = pyvisa.ResourceManager()
    awg = rm.open_resource(res)
    awg.timeout = 15000  # 15 seconds timeout for large data transfers

    try:
        # =====================================================
        # EXAMPLE USAGE: Change these parameters as you like!
        # =====================================================
        # Move from (0°, 0°) to (5°, 3°) over 2.0 seconds
        move_s_curve_direct(
            awg=awg,
            x1=0.0, y1=0.0,       # Start point (degrees)
            x2=5.0, y2=3.0,       # End point (degrees)
            duration_sec=2.0,     # Total time (seconds)
            sample_rate_hz=500,   # Safe USB rate (500 Hz = 1000 points for 2 sec)
            volt_per_deg=1.0      # Match your GVS002 jumper setting
        )

        # Optional: Wait a bit and move back to origin
        time.sleep(1.0)
        print("Moving back to origin...")
        move_s_curve_direct(
            awg=awg,
            x1=5.0, y1=3.0,
            x2=0.0, y2=0.0,
            duration_sec=1.5,
            sample_rate_hz=500,
            volt_per_deg=1.0
        )

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 1
    except Exception as e:
        print(f"\nError during motion: {e}")
        return 1
    finally:
        # Safety: Turn off both outputs and close the connection
        # Wrap each write in try-except to avoid timeout errors during shutdown
        print("Shutting down outputs...")
        try:
            awg.write(':OUTP1 OFF')
        except Exception:
            pass
        try:
            awg.write(':OUTP2 OFF')
        except Exception:
            pass
        try:
            awg.close()
        except Exception:
            pass
        print("Done.")


if __name__ == "__main__":
    sys.exit(main())
