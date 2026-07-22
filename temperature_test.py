#!/usr/bin/env python3
"""temperature_test.py - prove the TC LAB temperature controller works, alone.

NO ROS, no Docker, no gateway, no API key: just pyvisa + the TCLab class in
temperature.py. Run this FIRST, on whatever machine has the controller plugged
in (your laptop or the Pi). If this passes, the hardware and the driver are
good and anything that fails afterwards is integration, not instrument.

What it does, in order:
  1. finds/opens the instrument and prints its identity, firmware, sensor
  2. reads the sensor with the output still OFF  (proves the sensor path)
  3. checks your setpoints against the instrument's OWN temperature limits and
     refuses to run if they fall outside  (nothing gets driven by accident)
  4. enables the TEC output, drives to each setpoint in turn, and prints the
     live temperature until it settles inside --tolerance or --hold expires
  5. ALWAYS turns the output back off, hands the box back to its touchscreen,
     and drains the error queue

Usage:
    pip install pyvisa pyvisa-py pyusb          # + libusb on Linux for USB
    python temperature_test.py --list                       # what VISA sees
    python temperature_test.py                              # auto-find on USB
    python temperature_test.py --resource TCPIP::192.168.1.50::INSTR
    python temperature_test.py --setpoints 25,30 --hold 120 --tolerance 0.2
    python temperature_test.py --no-output                  # read-only, never drives

Env: TCLAB_RESOURCE is used when --resource is omitted.

SAFETY: --setpoints are degrees in the instrument's active unit (Celsius by
default). The script never touches your current/voltage limits -- set those on
the instrument for your TEC before running.
"""

import argparse
import os
import sys
import time

import pyvisa

from temperature import TCLab, TCLabError


def list_resources(backend="@py"):
    try:
        return list(pyvisa.ResourceManager(backend).list_resources())
    except Exception as exc:                      # no backend, no libusb, ...
        print(f"  ! VISA could not enumerate resources: {exc}")
        return []


def pick_resource(explicit, backend="@py"):
    """--resource > $TCLAB_RESOURCE > the first USB instrument VISA can see.

    Network (TCPIP) instruments are NOT discoverable -- pyvisa-py cannot scan
    the LAN -- so an Ethernet TC LAB must always be named explicitly.
    """
    chosen = explicit or os.environ.get("TCLAB_RESOURCE")
    if chosen:
        return chosen
    found = list_resources(backend)
    usb = [r for r in found if r.upper().startswith("USB")]
    if not usb:
        print("No USB instrument found. Seen by VISA:", found or "(nothing)")
        print("Pass --resource TCPIP::<ip>::INSTR for an Ethernet TC LAB.")
        return None
    if len(usb) > 1:
        print(f"  ! {len(usb)} USB instruments present; using the first: {usb}")
    return usb[0]


def read_block(tc, label):
    """One labelled snapshot of everything the box will tell us right now."""
    st = tc.status()
    flags = [name for name, on in (("in-tolerance", st["in_tolerance"]),
                                   ("output-active", st["output_active"]),
                                   ("current-limit", st["current_limit"]),
                                   ("SENSOR-OPEN", st["sensor_open"]),
                                   ("SENSOR-SHORT", st["sensor_short"]),
                                   ("temp-hi-limit", st["temp_hi_limit"]),
                                   ("temp-lo-limit", st["temp_lo_limit"])) if on]
    print(f"{label:<22} T={st['actual']:8.3f}   setpoint={st['setpoint']:8.3f}   "
          f"I={st['tec_current']:6.3f} A   V={st['tec_voltage']:6.3f} V   "
          f"[{', '.join(flags) or 'no flags'}]")
    return st


def drive_to(tc, target, hold_s, tolerance, settle_s, poll_s):
    """Set one setpoint and watch the sensor until it settles or time runs out.

    'Settled' here is OUR criterion (|T - target| <= tolerance held for
    settle_s), deliberately independent of the instrument's own tolerance flag
    -- we print that flag too, so you can see whether they agree.
    """
    print(f"\n--- setpoint -> {target:g} ---")
    tc.set_setpoint(target)
    deadline = time.time() + hold_s
    inside_since = None
    while time.time() < deadline:
        actual = tc.temperature()
        error = actual - target
        inside = abs(error) <= tolerance
        if inside and inside_since is None:
            inside_since = time.time()
        elif not inside:
            inside_since = None
        settled_for = (time.time() - inside_since) if inside_since else 0.0
        print(f"  t+{hold_s - (deadline - time.time()):5.0f}s  T={actual:8.3f}  "
              f"err={error:+7.3f}  I={tc.tec_current():6.3f} A"
              f"{'  in-band' if inside else ''}"
              f"{f' {settled_for:.0f}s' if inside_since else ''}",
              flush=True)
        if settled_for >= settle_s:
            print(f"  SETTLED at {actual:.3f} "
                  f"(within {tolerance:g} for {settle_s:g} s; "
                  f"instrument in-tolerance flag: {tc.in_tolerance()})")
            return True, actual
        time.sleep(poll_s)
    actual = tc.temperature()
    print(f"  NOT SETTLED after {hold_s:g} s: T={actual:.3f}, "
          f"err={actual - target:+.3f} (raise --hold, or check the TEC/limits)")
    return False, actual


def main():
    p = argparse.ArgumentParser(
        description="Standalone bench test for the Wavelength TC LAB controller.")
    p.add_argument("--resource", default=None,
                   help="VISA resource (default: $TCLAB_RESOURCE, else first USB)")
    p.add_argument("--list", action="store_true",
                   help="list the VISA resources this machine can see, then exit")
    p.add_argument("--setpoints", default="25,30",
                   help="comma-separated setpoints to drive to, in order (default 25,30)")
    p.add_argument("--hold", type=float, default=120.0,
                   help="max seconds to wait at each setpoint (default 120)")
    p.add_argument("--tolerance", type=float, default=0.2,
                   help="degrees counted as 'reached' (default 0.2)")
    p.add_argument("--settle", type=float, default=10.0,
                   help="seconds inside tolerance before calling it settled (default 10)")
    p.add_argument("--poll", type=float, default=2.0,
                   help="seconds between readings (default 2)")
    p.add_argument("--no-output", action="store_true",
                   help="never enable the TEC output: connect + read only")
    p.add_argument("--timeout-ms", type=int, default=5000, help="VISA I/O timeout")
    args = p.parse_args()

    if args.list:
        print("VISA resources:")
        for r in list_resources() or ["(nothing)"]:
            print("  ", r)
        return 0

    resource = pick_resource(args.resource)
    if not resource:
        return 2
    try:
        setpoints = [float(s) for s in args.setpoints.split(",") if s.strip()]
    except ValueError:
        print(f"Bad --setpoints: {args.setpoints!r}")
        return 2

    print(f"Connecting to {resource} ...")
    try:
        tc = TCLab(resource, timeout_ms=args.timeout_ms)
    except (TCLabError, pyvisa.errors.VisaIOError, OSError) as exc:
        print(f"FAILED to open {resource}: {exc}")
        print("Checks: instrument powered and its rear switch on; USB cable in; "
              "on Linux libusb installed and udev permissions for the device; "
              "for Ethernet, the IP is reachable (ping it).")
        return 1

    ok = True
    with tc:
        # --- 1. identity -------------------------------------------------
        print("  IDN        :", tc.idn)
        print("  serial     :", tc.serial_number())
        print("  firmware   :", tc.firmware_version())
        print("  sensor     :", tc.get_sensor_coeffs())
        tc.set_units(TCLab.CELSIUS)          # everything below is degrees C
        print("  units      : Celsius (forced)")

        # --- 2. cold read ------------------------------------------------
        print()
        cold = read_block(tc, "before anything:")
        if cold["sensor_open"] or cold["sensor_short"]:
            print("\nSENSOR FAULT (open/short). Fix the sensor wiring before driving "
                  "the TEC -- refusing to enable the output.")
            print("Errors:", tc.get_errors() or "clean")
            return 1

        # --- 3. limits ---------------------------------------------------
        t_lo, t_hi = tc.get_temp_limit_low(), tc.get_temp_limit_high()
        i_pos, i_neg = tc.get_current_limit_pos(), tc.get_current_limit_neg()
        print(f"  limits     : T {t_lo:g} .. {t_hi:g}   I +{i_pos:g} / -{i_neg:g} A")
        outside = [s for s in setpoints if not (t_lo <= s <= t_hi)]
        if outside:
            print(f"\nRefusing to run: setpoint(s) {outside} are outside the "
                  f"instrument's own limits ({t_lo:g}..{t_hi:g}). Change --setpoints, "
                  f"or the limits on the instrument if they are wrong for your TEC.")
            return 2

        if args.no_output:
            print("\n--no-output: skipping the drive test. Sensor path verified above.")
            print("Errors:", tc.get_errors() or "clean")
            return 0

        # --- 4. drive ----------------------------------------------------
        try:
            tc.output(True)
            time.sleep(0.5)
            if not tc.output_enabled():
                # The rear Remote Enable input (DB-9 pin 1) gates the output.
                print("\nOutput did NOT enable. It is gated by the rear Remote Enable "
                      "input; default polarity is ENABLE-HI with a pull-up, so check "
                      "TEC:INTPOL / anything wired to the DB-9.")
                return 1
            print("  output     : ON")
            for target in setpoints:
                settled, _ = drive_to(tc, target, args.hold, args.tolerance,
                                      args.settle, args.poll)
                ok = ok and settled
        except KeyboardInterrupt:
            print("\nInterrupted.")
            ok = False
        finally:
            # 5. Never leave a TEC driving an unattended cell.
            try:
                tc.output(False)
                print("\n  output     : OFF")
            except TCLabError as exc:
                print(f"\n  ! could not turn the output off: {exc}")
            read_block(tc, "after test:")
            errs = tc.get_errors()
            print("  errors     :", errs or "clean")
            print("  reconnects :", tc.reconnects)
            ok = ok and not errs

    print("\nRESULT:", "PASS - the controller works." if ok else
          "FAIL - see the lines above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
