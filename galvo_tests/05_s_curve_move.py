"""
Step 5 - Smooth S-Curve point-to-point movement via direct /dev/usbtmc.

This script bypasses PyVISA and writes directly to the USBTMC character device
to avoid USB bulk transfer timeouts. Works reliably on Raspberry Pi.

Usage:
    python galvo_tests/05_s_curve_move.py
"""

import sys
import time
import numpy as np
import os
import glob


def find_usbtmc_device():
    """Find the first /dev/usbtmc* device (Rigol DG1000Z)."""
    devices = glob.glob('/dev/usbtmc*')
    if not devices:
        return None
    return devices[0]


def write_scpi(dev_path, command):
    """Write a SCPI command string (append newline) to the USBTMC device."""
    with open(dev_path, 'wb') as f:
        f.write((command + '\n').encode())


def query_scpi(dev_path, command, timeout=5.0):
    """
    Write a SCPI query and read the response.
    This is a simple implementation; for large reads we may need to handle partial.
    """
    with open(dev_path, 'wb') as f:
        f.write((command + '\n').encode())
    time.sleep(0.1)  # Give the instrument time to respond
    with open(dev_path, 'rb') as f:
        # Read up to 4096 bytes; in practice *IDN? returns < 100 bytes
        return f.read(4096).decode().strip()


def move_s_curve_direct(dev_path, x1, y1, x2, y2, duration_sec,
                        sample_rate_hz=50, s_factor=4.0, volt_per_deg=1.0):
    """
    Move the galvo using S-curve via arbitrary waveform, direct USBTMC write.
    """
    if duration_sec <= 0:
        write_scpi(dev_path, f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        write_scpi(dev_path, f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        write_scpi(dev_path, ':OUTP1 ON')
        write_scpi(dev_path, ':OUTP2 ON')
        return

    # 1. Calculate total points (keep it small to be safe)
    total_points = int(duration_sec * sample_rate_hz)
    if total_points > 16384:
        sample_rate_hz = 16384 / duration_sec
        total_points = 16384
    elif total_points < 10:
        total_points = 10
        sample_rate_hz = total_points / duration_sec

    print(f"Points: {total_points}, Sample rate: {sample_rate_hz:.0f} Hz")

    # 2. Generate tanh S-curve
    t = np.linspace(-s_factor, s_factor, total_points)
    normalized_pos = (np.tanh(t) + 1.0) / 2.0

    # 3. Voltages
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

    # 5. Convert to comma-separated ASCII strings (small enough)
    x_data = ",".join(map(str, x_dac))
    y_data = ",".join(map(str, y_dac))
    print(f"X data length: {len(x_data)} chars, Y data length: {len(y_data)} chars")

    print(f"Moving: ({x1:.2f}, {y1:.2f}) -> ({x2:.2f}, {y2:.2f}) in {duration_sec}s...")

    # 6. Upload X
    write_scpi(dev_path, ':SOUR1:FUNC:SHAP ARB')
    write_scpi(dev_path, f':SOUR1:FUNC:ARB:SRATE {sample_rate_hz:.0f}')
    write_scpi(dev_path, f':SOUR1:TRACE:DATA VOLATILE,{x_data}')
    time.sleep(0.2)  # Allow processing

    # 7. Upload Y
    write_scpi(dev_path, ':SOUR2:FUNC:SHAP ARB')
    write_scpi(dev_path, f':SOUR2:FUNC:ARB:SRATE {sample_rate_hz:.0f}')
    write_scpi(dev_path, f':SOUR2:TRACE:DATA VOLATILE,{y_data}')
    time.sleep(0.2)

    # 8. Enable outputs
    write_scpi(dev_path, ':SOUR1:VOLT 20')
    write_scpi(dev_path, ':SOUR1:VOLT:OFFS 0')
    write_scpi(dev_path, ':OUTP1 ON')

    write_scpi(dev_path, ':SOUR2:VOLT 20')
    write_scpi(dev_path, ':SOUR2:VOLT:OFFS 0')
    write_scpi(dev_path, ':OUTP2 ON')

    # 9. Wait for completion
    time.sleep(duration_sec + 0.1)

    # 10. Hold final position
    write_scpi(dev_path, ':SOUR1:FUNC:SHAP DC')
    write_scpi(dev_path, f':SOUR1:VOLT:OFFS {vx2}')
    write_scpi(dev_path, ':SOUR2:FUNC:SHAP DC')
    write_scpi(dev_path, f':SOUR2:VOLT:OFFS {vy2}')
    print("Motion completed.")


def main():
    # Find the USBTMC device
    dev = find_usbtmc_device()
    if not dev:
        print("ERROR: No /dev/usbtmc* device found. Is the DG1022Z connected?")
        return 1

    print(f"Using USBTMC device: {dev}")

    # Send a reset to ensure known state
    write_scpi(dev, '*RST')
    time.sleep(0.5)

    # Test communication
    idn = query_scpi(dev, '*IDN?')
    print(f"IDN: {idn}")

    try:
        # Example: Move from (0,0) to (5,3) in 2 seconds
        move_s_curve_direct(
            dev_path=dev,
            x1=0.0, y1=0.0,
            x2=5.0, y2=3.0,
            duration_sec=2.0,
            sample_rate_hz=50,     # 100 points, very small -> should succeed
            s_factor=4.0,
            volt_per_deg=1.0
        )

        time.sleep(1.0)
        print("\nMoving back to origin...")
        move_s_curve_direct(
            dev_path=dev,
            x1=5.0, y1=3.0,
            x2=0.0, y2=0.0,
            duration_sec=1.5,
            sample_rate_hz=50,
            s_factor=4.0,
            volt_per_deg=1.0
        )

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 1
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Turn off outputs
        print("Shutting down outputs...")
        try:
            write_scpi(dev, ':OUTP1 OFF')
        except Exception:
            pass
        try:
            write_scpi(dev, ':OUTP2 OFF')
        except Exception:
            pass
        print("Done.")


if __name__ == "__main__":
    sys.exit(main())
