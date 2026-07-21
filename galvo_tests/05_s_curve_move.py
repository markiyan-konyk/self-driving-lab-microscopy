#!/usr/bin/env python3
"""
Step 5 - Smooth S-Curve movement using PyVISA with chunked transfer.

This version uses PyVISA (like 01_connect.py) but splits the waveform data
into tiny chunks to avoid USB timeout. Works reliably on Raspberry Pi.

Usage:
    python3 05_s_curve_move.py
"""

import sys
import time
import numpy as np
import pyvisa
from _common import resolve_resource


def move_s_curve_pyvisa(awg, x1, y1, x2, y2, duration,
                        sample_rate=10, s_factor=4.0, volt_per_deg=1.0,
                        chunk_size=10):
    """
    Move using S-curve with PyVISA and chunked data transfer.

    Args:
        awg: PyVISA resource object (already open).
        x1, y1: Start coordinates (degrees).
        x2, y2: End coordinates (degrees).
        duration: Total time (seconds).
        sample_rate: Points per second (low values reduce data size).
        s_factor: S-curve steepness (4.0 is optimal).
        volt_per_deg: Galvo scaling (1.0, 0.8, or 0.5).
        chunk_size: Number of points per USB write (smaller = safer).
    """
    if duration <= 0:
        awg.write(f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        awg.write(f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        awg.write(':OUTP1 ON')
        awg.write(':OUTP2 ON')
        return

    # 1. Calculate total points (capped at 30 to be ultra-safe)
    total_points = int(duration * sample_rate)
    MAX_POINTS = 30
    if total_points > MAX_POINTS:
        total_points = MAX_POINTS
        sample_rate = total_points / duration
    elif total_points < 5:
        total_points = 5
        sample_rate = total_points / duration

    print(f"Points: {total_points}, sample rate: {sample_rate:.1f} Hz")

    # 2. Generate S-curve weights
    t = np.linspace(-s_factor, s_factor, total_points)
    weights = (np.tanh(t) + 1.0) / 2.0

    # 3. Compute voltages
    vx1 = x1 * volt_per_deg
    vx2 = x2 * volt_per_deg
    vy1 = y1 * volt_per_deg
    vy2 = y2 * volt_per_deg

    x_volts = vx1 + (vx2 - vx1) * weights
    y_volts = vy1 + (vy2 - vy1) * weights

    # 4. Convert to DAC integers (0..16383 for -10V..+10V)
    def to_dac(v):
        return np.clip(((v + 10.0) / 20.0) * 16383, 0, 16383).astype(int)

    x_dac = to_dac(x_volts)
    y_dac = to_dac(y_volts)

    print(f"Moving ({x1:.1f},{y1:.1f}) -> ({x2:.1f},{y2:.1f}) in {duration}s...")

    # 5. Upload X channel in chunks
    awg.write(':SOUR1:FUNC:SHAP ARB')
    awg.write(f':SOUR1:FUNC:ARB:SRATE {sample_rate:.0f}')
    for i in range(0, len(x_dac), chunk_size):
        chunk = x_dac[i:i+chunk_size]
        cmd = f':SOUR1:TRACE:DATA VOLATILE,{",".join(map(str, chunk))}'
        awg.write(cmd)
        time.sleep(0.05)   # Small delay to let DG1000Z process each chunk

    # 6. Upload Y channel in chunks
    awg.write(':SOUR2:FUNC:SHAP ARB')
    awg.write(f':SOUR2:FUNC:ARB:SRATE {sample_rate:.0f}')
    for i in range(0, len(y_dac), chunk_size):
        chunk = y_dac[i:i+chunk_size]
        cmd = f':SOUR2:TRACE:DATA VOLATILE,{",".join(map(str, chunk))}'
        awg.write(cmd)
        time.sleep(0.05)

    # 7. Set amplitude and enable outputs
    awg.write(':SOUR1:VOLT 20')
    awg.write(':SOUR1:VOLT:OFFS 0')
    awg.write(':OUTP1 ON')

    awg.write(':SOUR2:VOLT 20')
    awg.write(':SOUR2:VOLT:OFFS 0')
    awg.write(':OUTP2 ON')

    # 8. Wait for motion to finish
    time.sleep(duration + 0.1)

    # 9. Hold final position (DC)
    awg.write(':SOUR1:FUNC:SHAP DC')
    awg.write(f':SOUR1:VOLT:OFFS {vx2}')
    awg.write(':SOUR2:FUNC:SHAP DC')
    awg.write(f':SOUR2:VOLT:OFFS {vy2}')
    print("Motion completed.")


def main():
    # Find the device using the same logic as 01_connect.py
    res = resolve_resource()
    if not res:
        print("ERROR: No galvo resource found. Is DG1000Z connected?")
        return 1

    print(f"Connecting to {res} ...")
    rm = pyvisa.ResourceManager()
    awg = rm.open_resource(res)
    awg.timeout = 15000   # 15 seconds (more than enough for tiny chunks)

    # Test communication
    try:
        idn = awg.query('*IDN?').strip()
        print(f"IDN: {idn}")
    except Exception as e:
        print(f"Communication error: {e}")
        awg.close()
        return 1

    try:
        # Move from (0,0) to (5,3) in 2 seconds with only 20 points
        move_s_curve_pyvisa(
            awg=awg,
            x1=0.0, y1=0.0,
            x2=5.0, y2=3.0,
            duration=2.0,
            sample_rate=10,      # 2s * 10Hz = 20 points
            s_factor=4.0,
            volt_per_deg=1.0,
            chunk_size=10        # send 10 points per USB write
        )

        time.sleep(1.0)
        print("\nMoving back to origin...")
        move_s_curve_pyvisa(
            awg=awg,
            x1=5.0, y1=3.0,
            x2=0.0, y2=0.0,
            duration=1.5,
            sample_rate=10,
            s_factor=4.0,
            volt_per_deg=1.0,
            chunk_size=10
        )

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Safety: turn off outputs and close connection
        print("Shutting down outputs...")
        try:
            awg.write(':OUTP1 OFF')
        except:
            pass
        try:
            awg.write(':OUTP2 OFF')
        except:
            pass
        awg.close()
        print("Done.")

if __name__ == "__main__":
    sys.exit(main())
