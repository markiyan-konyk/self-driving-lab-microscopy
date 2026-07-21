#!/usr/bin/env python3
"""
Step 5 - Smooth S-Curve via direct /dev/usbtmc (Kernel driver).

Improved version: properly stops output before switching to DC.
"""

import sys
import time
import numpy as np
import glob


def find_usbtmc():
    devices = glob.glob('/dev/usbtmc*')
    return devices[0] if devices else None


def write_cmd(dev, cmd):
    with open(dev, 'wb') as f:
        f.write((cmd + '\n').encode())


def query_cmd(dev, cmd):
    with open(dev, 'wb') as f:
        f.write((cmd + '\n').encode())
    time.sleep(0.1)
    with open(dev, 'rb') as f:
        return f.read(4096).decode().strip()


def move_s_curve_direct(dev, x1, y1, x2, y2, duration,
                        sample_rate=5, s_factor=4.0, volt_per_deg=1.0):
    """
    Move using S-curve with safe output handling.
    """
    if duration <= 0:
        write_cmd(dev, f':SOUR1:APPL:DC {x1 * volt_per_deg}')
        write_cmd(dev, f':SOUR2:APPL:DC {y1 * volt_per_deg}')
        write_cmd(dev, ':OUTP1 ON')
        write_cmd(dev, ':OUTP2 ON')
        return

    # 1. Turn off outputs before changing anything
    write_cmd(dev, ':OUTP1 OFF')
    write_cmd(dev, ':OUTP2 OFF')
    time.sleep(0.1)

    # 2. Total points (capped at 20 for absolute safety)
    total_points = int(duration * sample_rate)
    MAX_POINTS = 20
    if total_points > MAX_POINTS:
        total_points = MAX_POINTS
        sample_rate = total_points / duration
    elif total_points < 5:
        total_points = 5
        sample_rate = total_points / duration

    print(f"Points: {total_points}, sample rate: {sample_rate:.1f} Hz")

    # 3. Generate S-curve
    t = np.linspace(-s_factor, s_factor, total_points)
    weights = (np.tanh(t) + 1.0) / 2.0

    vx1 = x1 * volt_per_deg
    vx2 = x2 * volt_per_deg
    vy1 = y1 * volt_per_deg
    vy2 = y2 * volt_per_deg

    x_volts = vx1 + (vx2 - vx1) * weights
    y_volts = vy1 + (vy2 - vy1) * weights

    def to_dac(v):
        return np.clip(((v + 10.0) / 20.0) * 16383, 0, 16383).astype(int)

    x_dac = to_dac(x_volts)
    y_dac = to_dac(y_volts)

    x_str = ",".join(map(str, x_dac))
    y_str = ",".join(map(str, y_dac))
    print(f"X data size: {len(x_str)} bytes, Y data size: {len(y_str)} bytes")

    print(f"Moving ({x1:.1f},{y1:.1f}) -> ({x2:.1f},{y2:.1f}) in {duration}s...")

    # 4. Upload X
    write_cmd(dev, ':SOUR1:FUNC:SHAP ARB')
    write_cmd(dev, f':SOUR1:FUNC:ARB:SRATE {sample_rate:.0f}')
    write_cmd(dev, f':SOUR1:TRACE:DATA VOLATILE,{x_str}')
    time.sleep(0.2)

    # 5. Upload Y
    write_cmd(dev, ':SOUR2:FUNC:SHAP ARB')
    write_cmd(dev, f':SOUR2:FUNC:ARB:SRATE {sample_rate:.0f}')
    write_cmd(dev, f':SOUR2:TRACE:DATA VOLATILE,{y_str}')
    time.sleep(0.2)

    # 6. Set amplitude and enable outputs
    write_cmd(dev, ':SOUR1:VOLT 20')
    write_cmd(dev, ':SOUR1:VOLT:OFFS 0')
    write_cmd(dev, ':OUTP1 ON')

    write_cmd(dev, ':SOUR2:VOLT 20')
    write_cmd(dev, ':SOUR2:VOLT:OFFS 0')
    write_cmd(dev, ':OUTP2 ON')

    # 7. Let the waveform play for the specified duration
    time.sleep(duration + 0.1)

    # 8. Safely stop output and switch to DC hold
    print("Holding final position...")
    write_cmd(dev, ':OUTP1 OFF')
    write_cmd(dev, ':OUTP2 OFF')
    time.sleep(0.1)

    write_cmd(dev, ':SOUR1:FUNC:SHAP DC')
    write_cmd(dev, f':SOUR1:VOLT:OFFS {vx2}')
    write_cmd(dev, ':SOUR2:FUNC:SHAP DC')
    write_cmd(dev, f':SOUR2:VOLT:OFFS {vy2}')
    time.sleep(0.1)

    write_cmd(dev, ':OUTP1 ON')
    write_cmd(dev, ':OUTP2 ON')
    print("Motion completed.")


def main():
    dev = find_usbtmc()
    if not dev:
        print("ERROR: No /dev/usbtmc* found.")
        print("Create it with: sudo mknod /dev/usbtmc0 c 180 0")
        return 1

    print(f"Using USBTMC device: {dev}")

    # Reset instrument
    write_cmd(dev, '*RST')
    time.sleep(0.5)

    # Test communication
    idn = query_cmd(dev, '*IDN?')
    print(f"IDN: {idn}")

    try:
        # Move from (0,0) to (5,3) in 2 seconds (10 points)
        move_s_curve_direct(
            dev=dev,
            x1=0.0, y1=0.0,
            x2=5.0, y2=3.0,
            duration=2.0,
            sample_rate=5,       # 2s * 5Hz = 10 points
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
            sample_rate=5,
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
