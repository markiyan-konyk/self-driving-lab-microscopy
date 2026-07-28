"""
Minimal connectivity test for the Wavelength Electronics TC10 LAB.

Scans every VISA resource, asks each one *IDN?, picks the TC LAB (ignoring the
Rigol wavegen), then reads the temperature sensor.

    pip install pyvisa pyvisa-py pyusb
    # Linux/Pi also needs libusb + udev access to the USBTMC device:
    #   sudo apt install libusb-1.0-0

    python3 tc10_read.py                    # scan and auto-pick
    python3 tc10_read.py USB0::0x...::INSTR # skip the scan, use this address
    python3 tc10_read.py --all              # also probe ASRL serial ports

Each resource is probed in a throwaway thread: a device whose open() hangs
inside libusb cannot be interrupted from Python, so we abandon it and move on
instead of freezing the whole script.
"""

import sys
import threading
import pyvisa

PROBE_TIMEOUT_S = 4.0

rm = pyvisa.ResourceManager("@py")
args = [a for a in sys.argv[1:] if not a.startswith("-")]
probe_serial = "--all" in sys.argv

print(f"pyvisa {pyvisa.__version__}, backend: {rm.visalib}")


def probe(res, result):
    """Open one resource and read its *IDN?. Runs in its own thread."""
    try:
        inst = rm.open_resource(res, open_timeout=int(PROBE_TIMEOUT_S * 1000))
        inst.timeout = int(PROBE_TIMEOUT_S * 1000)
        result["idn"] = inst.query("*IDN?").strip()
        result["inst"] = inst
    except Exception as e:
        result["err"] = e


tc = None

if args:
    resources = [args[0]]
    print(f"\nUsing address from command line: {args[0]}")
else:
    print("\nListing resources...", flush=True)
    resources = list(rm.list_resources())
    if not resources:
        print("  (none)")
    for r in resources:
        print(f"  {r}")

print()
for res in resources:
    if res.startswith("ASRL") and not probe_serial and not args:
        print(f"{res:<40} -- skipped (serial port; use --all to probe)")
        continue

    print(f"{res:<40} -- probing...", end=" ", flush=True)
    result = {}
    t = threading.Thread(target=probe, args=(res, result), daemon=True)
    t.start()
    t.join(PROBE_TIMEOUT_S + 2)

    if t.is_alive():
        print("HUNG, abandoned (probably a stuck libusb open)")
        continue
    if "err" in result:
        print(f"no reply ({type(result['err']).__name__}: {result['err']})")
        continue

    idn = result["idn"]
    print(idn)

    up = idn.upper()
    if "TC10" in up or "WAVELENGTH" in up:
        tc = result["inst"]
    else:
        result["inst"].close()

if tc is None:
    print("\nNo TC10 LAB found.")
    print("Things to check on the Pi:")
    print("  lsusb                      -- does the TC10 enumerate at all?")
    print("  lsmod | grep usbtmc        -- if loaded, the kernel owns the device")
    print("                                and libusb cannot: sudo rmmod usbtmc")
    print("  ls -l /dev/usbtmc*         -- kernel-driver path; permissions?")
    print("  udev rule / plugdev group  -- see the install notes")
    print("  or pass the address directly:  python3 tc10_read.py 'TCPIP::<ip>::INSTR'")
    raise SystemExit(1)

print(f"\nUsing: {tc.resource_name}")

units = {"0": "C", "1": "K", "2": "F", "3": "raw"}.get(tc.query("TEC:UNITS?").strip(), "?")
print(f"Sensor      : {tc.query('TEC:SENSOR?').strip()}")
print(f"Setpoint    : {tc.query('TEC:SET?').strip()} {units}")
print(f"Temperature : {tc.query('TEC:ACT?').strip()} {units}")
print(f"TEC output  : {'ON' if tc.query('TEC:OUTput?').strip() == '1' else 'OFF'}")
print(f"Errors      : {tc.query('ERRSTR?').strip()}")

tc.write("LOCAL")   # hand the front panel back
tc.close()
