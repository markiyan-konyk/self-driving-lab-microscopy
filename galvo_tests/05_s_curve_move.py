#!/usr/bin/env python3
"""
Step 5 - Smooth S-Curve via direct /dev/usbtmc (Kernel driver, NO PyVISA).

This bypasses libusb/PyVISA entirely and uses the stable Linux usbtmc driver.
Requires /dev/usbtmc0 to exist (create with: sudo mknod /dev/usbtmc0 c 180 0).

Usage:
    python3 05_s_curve_move.py
"""

import sys
import time
import numpy as np
import glob


def find_usbtmc():
    """Return the first /dev/usbtmc* device path."""
    devices = glob.glob('/dev/usbtmc*')
    return devices[0] if devices else None


def write_cmd(dev, cmd):
    """Write a SCPI command (append newline) to the USBTMC device."""
    with open(dev, 'wb') as f:
        f.write((cmd + '\n').encode())


def query_cmd(dev, cmd):
    """Write a query and read the response."""
    with open(dev, 'wb') as f:
        f.write((cmd + '\n').encode())
    time.sleep(0.1)  # Give the instrument time to respond
    with open(dev, 'rb') as f:
        return f.read(4096).decode().strip()


def move_s_curve_direct(dev, x1, y1, x2, y2, duration,
                        sample_rate=10, s_factor=4.0, volt_per_deg=1.0):
    """
    Move using S-curve via direct kernel USBTMC driver.
    Ultra-reliable, no USB timeouts.
    """
    if duration <= 0:
        write_cmd(dev, f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        write_cmd(dev, f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        write_cmd(dev, ':OUTP1 ON')
        write_cmd(dev, ':OUTP2 ON')
        return

    # 1. Total points (capped at 30 for absolute safety)
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

    # 5. Convert to comma-separated string (only ~120 bytes for 20 points)
    x_str = ",".join(map(str, x_dac))
    y_str = ",".join(map(str, y_dac))
    print(f"X data size: {len(x_str)} bytes, Y data size: {len(y_str)} bytes")

    print(f"Moving ({x1:.1f},{y1:.1f}) -> ({x2:.1f},{y2:.1f}) in {duration}s...")

    # 6. Upload X
    write_cmd(dev, ':SOUR1:FUNC:SHAP ARB')
    write_cmd(dev, f':SOUR1:FUNC:ARB:SRATE {sample_rate:.0f}')
    write_cmd(dev, f':SOUR1:TRACE:DATA VOLATILE,{x_str}')
    time.sleep(0.2)  # Critical: let the instrument process

    # 7. Upload Y
    write_cmd(dev, ':SOUR2:FUNC:SHAP ARB')
    write_cmd(dev, f':SOUR2:FUNC:ARB:SRATE {sample_rate:.0f}')
    write_cmd(dev, f':SOUR2:TRACE:DATA VOLATILE,{y_str}')
    time.sleep(0.2)

    # 8. Enable outputs
    write_cmd(dev, ':SOUR1:VOLT 20')
    write_cmd(dev, ':SOUR1:VOLT:OFFS 0')
    write_cmd(dev, ':OUTP1 ON')

    write_cmd(dev, ':SOUR2:VOLT 20')
    write_cmd(dev, ':SOUR2:VOLT:OFFS 0')
    write_cmd(dev, ':OUTP2 ON')

    # 9. Wait for motion
    time.sleep(duration + 0.1)

    # 10. Hold final position (DC)
    write_cmd(dev, ':SOUR1:FUNC:SHAP DC')
    write_cmd(dev, f':SOUR1:VOLT:OFFS {vx2}')
    write_cmd(dev, ':SOUR2:FUNC:SHAP DC')
    write_cmd(dev, f':SOUR2:VOLT:OFFS {vy2}')
    print("Motion completed.")


def main():
    dev = find_usbtmc()
    if not dev:
        print("ERROR: No /dev/usbtmc* found.")
        print("Create it with: sudo mknod /dev/usbtmc0 c 180 0")
        return 1

    print(f"Using USBTMC device: {dev}")

    # Test communication
    write_cmd(dev, '*RST')
    time.sleep(0.5)
    idn = query_cmd(dev, '*IDN?')
    print(f"IDN: {idn}")

    try:
        # Move from (0,0) to (5,3) in 2 seconds
        move_s_curve_direct(
            dev=dev,
            x1=0.0, y1=0.0,
            x2=5.0, y2=3.0,
            duration=2.0,
            sample_rate=10,      # 20 points
            s_factor=4.0,
            volt_per_deg=1.0
        )

        time.sleep(1.0)
        print("\nMoving back to origin...")
        move_s_curve_direct(
            dev=dev,
            x1=5.0, y1=3.0,
            x2=0.0, y2=0.0,
            duration=1.5,
            sample_rate=10,
            s_factor=4.0,
            volt_per_deg=1.0
        )

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Shutting down outputs...")
        try:
            write_cmd(dev, ':OUTP1 OFF')
        except:
            pass
        try:
            write_cmd(dev, ':OUTP2 OFF')
        except:
            pass
        print("Done.")

if __name__ == "__main__":
    sys.exit(main())
