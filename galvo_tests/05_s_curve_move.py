"""
Step 5 - Smooth S-Curve point-to-point movement using Binary Block Transfer.

This script moves the laser from point A to point B over N seconds using a
tanh S-curve profile. Uses IEEE 488.2 binary block data for maximum USB speed.

Debugged version: removed *OPC? which caused timeout on DG1022Z firmware 03.01.12.
"""

import sys
import time
import numpy as np
import pyvisa
from _common import resolve_resource


def dac_to_binary_block(dac_values):
    """
    Convert DAC integers (0~16383) to IEEE 488.2 binary block.
    Format: #<len_digits><length><data> (Big Endian, 2 bytes per point)
    Example: #3811... (where 811 is the byte count)
    """
    byte_data = bytearray()
    for val in dac_values:
        byte_data.append((val >> 8) & 0xFF)  # High byte
        byte_data.append(val & 0xFF)         # Low byte
    
    total_len = len(byte_data)
    len_digits = len(str(total_len))
    # Header: # + len_digits + total_len
    header = f"#{len_digits}{total_len}".encode()
    return header + byte_data


def move_s_curve_direct(awg, x1, y1, x2, y2, duration_sec,
                        sample_rate_hz=100, s_factor=4.0, volt_per_deg=1.0):
    """
    Move the galvo from (x1, y1) to (x2, y2) using a tanh S-curve profile.
    Uses binary block transfer with no *OPC? query to avoid firmware hangs.
    """
    if duration_sec <= 0:
        awg.write(f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        awg.write(f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        awg.write(':OUTP1 ON')
        awg.write(':OUTP2 ON')
        return

    # 1. Calculate total points
    total_points = int(duration_sec * sample_rate_hz)
    if total_points > 16384:
        sample_rate_hz = 16384 / duration_sec
        total_points = 16384
        print(f"Adjusted sample rate to {sample_rate_hz:.1f} Hz")
    elif total_points < 10:
        total_points = 10
        sample_rate_hz = total_points / duration_sec

    print(f"Points: {total_points}, Sample rate: {sample_rate_hz:.0f} Hz")

    # 2. Generate tanh S-curve (0 -> 1)
    t = np.linspace(-s_factor, s_factor, total_points)
    normalized_pos = (np.tanh(t) + 1.0) / 2.0

    # 3. Convert degrees to voltages
    vx1 = x1 * volt_per_deg
    vx2 = x2 * volt_per_deg
    vy1 = y1 * volt_per_deg
    vy2 = y2 * volt_per_deg

    x_volts = vx1 + (vx2 - vx1) * normalized_pos
    y_volts = vy1 + (vy2 - vy1) * normalized_pos

    # 4. Convert to DAC integers (0~16383)
    def volts_to_dac(v):
        return np.clip(((v + 10.0) / 20.0) * 16383, 0, 16383).astype(int)

    x_dac = volts_to_dac(x_volts)
    y_dac = volts_to_dac(y_volts)

    # 5. Create binary blocks
    x_block = dac_to_binary_block(x_dac)
    y_block = dac_to_binary_block(y_dac)
    
    print(f"X block: {len(x_block)} bytes, Y block: {len(y_block)} bytes")
    print(f"Moving: ({x1:.2f}, {y1:.2f}) -> ({x2:.2f}, {y2:.2f}) in {duration_sec}s...")

    # 6. Upload X channel (CH1)
    print("  Sending X data...")
    awg.write(':SOUR1:FUNC:SHAP ARB')
    awg.write(f':SOUR1:FUNC:ARB:SRATE {sample_rate_hz:.0f}')
    awg.write_raw(b':SOUR1:TRACE:DATA VOLATILE,' + x_block)
    time.sleep(0.3)  # CRITICAL: Give the DG1022Z time to write to memory (no *OPC? here)

    # 7. Upload Y channel (CH2)
    print("  Sending Y data...")
    awg.write(':SOUR2:FUNC:SHAP ARB')
    awg.write(f':SOUR2:FUNC:ARB:SRATE {sample_rate_hz:.0f}')
    awg.write_raw(b':SOUR2:TRACE:DATA VOLATILE,' + y_block)
    time.sleep(0.3)  # CRITICAL: Give time to finish writing

    # 8. Set output parameters and enable
    print("  Enabling outputs...")
    awg.write(':SOUR1:VOLT 20')
    awg.write(':SOUR1:VOLT:OFFS 0')
    awg.write(':OUTP1 ON')

    awg.write(':SOUR2:VOLT 20')
    awg.write(':SOUR2:VOLT:OFFS 0')
    awg.write(':OUTP2 ON')

    # 9. Wait for motion to finish
    time.sleep(duration_sec + 0.1)

    # 10. Hold final position (DC mode)
    print("  Holding final position...")
    awg.write(':SOUR1:FUNC:SHAP DC')
    awg.write(f':SOUR1:VOLT:OFFS {vx2}')
    awg.write(':SOUR2:FUNC:SHAP DC')
    awg.write(f':SOUR2:VOLT:OFFS {vy2}')
    print("Motion completed.")


def main():
    res = resolve_resource()
    if not res:
        print("ERROR: No galvo resource found.")
        return 1

    print(f"Connecting to {res} ...")
    rm = pyvisa.ResourceManager()
    awg = rm.open_resource(res)
    awg.timeout = 10000  # 10 seconds is more than enough

    try:
        # Reset the instrument to a known state
        print("Resetting instrument...")
        awg.write('*RST')
        time.sleep(0.5)

        # =====================================================
        # EXAMPLE: Move from (0,0) to (5,3) in 2 seconds
        # =====================================================
        move_s_curve_direct(
            awg=awg,
            x1=0.0, y1=0.0,
            x2=5.0, y2=3.0,
            duration_sec=2.0,
            sample_rate_hz=100,   # Very safe, minimal data (~200 points)
            s_factor=4.0,
            volt_per_deg=1.0
        )

        # Wait a bit and move back
        time.sleep(1.0)
        print("\nMoving back to origin...")
        move_s_curve_direct(
            awg=awg,
            x1=5.0, y1=3.0,
            x2=0.0, y2=0.0,
            duration_sec=1.5,
            sample_rate_hz=100,
            s_factor=4.0,
            volt_per_deg=1.0
        )

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 1
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
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
