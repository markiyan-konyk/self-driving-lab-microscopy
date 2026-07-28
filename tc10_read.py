"""
Minimal connectivity test for the Wavelength Electronics TC10 LAB.

On this Pi the kernel's usbtmc driver claims the TC10 and exposes it as
/dev/usbtmc0, which stops libusb (and therefore pyvisa-py) from opening it --
that is the "HUNG" you get from a plain VISA scan. So we try the character
device first, which is just SCPI over a file handle, and fall back to VISA.

Needs read/write on /dev/usbtmc0:
    sudo tee /etc/udev/rules.d/99-usbtmc.rules >/dev/null <<'EOF'
    KERNEL=="usbtmc[0-9]*", MODE="0666"
    EOF
    sudo udevadm control --reload-rules && sudo udevadm trigger
    # then unplug/replug the TC10

    python3 tc10_read.py                     # auto-detect
    python3 tc10_read.py /dev/usbtmc0        # force this char device
    python3 tc10_read.py TCPIP::<ip>::INSTR  # force this VISA address
"""

import glob
import sys
import threading

PROBE_TIMEOUT_S = 4.0

args = [a for a in sys.argv[1:] if not a.startswith("-")]
probe_serial = "--all" in sys.argv


def open_usbtmc(path):
    """SCPI over the kernel usbtmc char device. Returns an ask() callable."""
    f = open(path, "r+b", buffering=0)

    def ask(cmd):
        f.write(cmd.encode() + b"\n")
        return f.read(4096).decode(errors="replace").strip() if cmd.endswith("?") else ""

    ask.close, ask.name = f.close, path
    return ask


def open_visa(res):
    """SCPI over pyvisa. Returns an ask() callable."""
    import pyvisa
    inst = rm.open_resource(res, open_timeout=int(PROBE_TIMEOUT_S * 1000))
    inst.timeout = int(PROBE_TIMEOUT_S * 1000)

    def ask(cmd):
        return inst.query(cmd).strip() if cmd.endswith("?") else (inst.write(cmd) and "")

    ask.close, ask.name = inst.close, res
    return ask


def probe(opener, target, result):
    """Open + *IDN? in a thread, so a stuck libusb call can be abandoned."""
    try:
        ask = opener(target)
        result["idn"] = ask("*IDN?")
        result["ask"] = ask
    except Exception as e:
        result["err"] = e


def try_open(opener, target):
    """Returns (idn, ask) or (None, None). Never blocks longer than the timeout."""
    print(f"{target:<40} -- probing...", end=" ", flush=True)
    result = {}
    t = threading.Thread(target=probe, args=(opener, target, result), daemon=True)
    t.start()
    t.join(PROBE_TIMEOUT_S + 2)

    if t.is_alive():
        print("HUNG, abandoned (stuck open; kernel driver likely owns it)")
    elif "err" in result:
        print(f"no reply ({type(result['err']).__name__}: {result['err']})")
    else:
        print(result["idn"])
        return result["idn"], result["ask"]
    return None, None


def is_tc10(idn):
    up = idn.upper()
    return "TC10" in up or "WAVELENGTH" in up


tc = None

# --- 1. kernel usbtmc char devices -----------------------------------------
targets = args if args else sorted(glob.glob("/dev/usbtmc*"))
if not args:
    print(f"usbtmc char devices: {targets or '(none)'}\n")

for path in [t for t in targets if t.startswith("/dev/")]:
    idn, ask = try_open(open_usbtmc, path)
    if ask and is_tc10(idn):
        tc = ask
        break
    elif ask:
        ask.close()

# --- 2. fall back to a VISA scan -------------------------------------------
if tc is None:
    import pyvisa
    rm = pyvisa.ResourceManager("@py")
    print(f"\npyvisa {pyvisa.__version__}, backend: {rm.visalib}")

    resources = [a for a in args if not a.startswith("/dev/")]
    if not resources:
        print("Listing VISA resources...", flush=True)
        resources = list(rm.list_resources())
        for r in resources:
            print(f"  {r}")
    print()

    for res in resources:
        if res.startswith("ASRL") and not probe_serial and not args:
            print(f"{res:<40} -- skipped (serial port; use --all to probe)")
            continue
        idn, ask = try_open(open_visa, res)
        if ask and is_tc10(idn):
            tc = ask
            break
        elif ask:
            ask.close()

if tc is None:
    print("\nNo TC10 LAB found.")
    print("  lsusb                              -- does it enumerate? (1a45:3101)")
    print("  ls -l /dev/usbtmc*                 -- need crw-rw-rw-, see udev rule above")
    print("  udevadm info -a -n /dev/usbtmc0 | grep -m1 idVendor   -- 1a45 = TC10")
    raise SystemExit(1)

# --- 3. read the thing -----------------------------------------------------
print(f"\nUsing: {tc.name}")

units = {"0": "C", "1": "K", "2": "F", "3": "raw"}.get(tc("TEC:UNITS?"), "?")
print(f"Sensor      : {tc('TEC:SENSOR?')}")
print(f"Setpoint    : {tc('TEC:SET?')} {units}")
print(f"Temperature : {tc('TEC:ACT?')} {units}")
print(f"TEC output  : {'ON' if tc('TEC:OUTput?') == '1' else 'OFF'}")
print(f"Errors      : {tc('ERRSTR?')}")

tc("LOCAL")   # hand the front panel back
tc.close()
