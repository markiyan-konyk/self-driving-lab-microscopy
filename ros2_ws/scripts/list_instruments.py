#!/usr/bin/env python3
"""Find the VISA addresses to put in ros2_ws/.env.

The nodes do NOT guess which instrument is which -- you name them once, here.
This prints every instrument it can see and the exact line to paste.

    # with the backend stopped (it holds the instruments while it runs):
    cd ros2_ws && docker compose down
    python3 scripts/list_instruments.py

    # ...or from inside the running container, if you prefer:
    docker compose exec scopio python3 /ros2_ws/scripts/list_instruments.py

"Resource busy" next to an instrument means something already owns it -- almost
always the backend. Stop it and run again.
"""

import glob
import os
import sys

# Instruments this rig knows about: vendor id -> (label, .env var, IDN markers).
KNOWN = {
    0x1AB1: ("Rigol DG1022Z (galvo mirrors)", "GALVO_RESOURCE", ("RIGOL", "DG1")),
    0x1A45: ("Wavelength TC10 LAB (temperature)", "TCLAB_RESOURCE", ("WAVELENGTH", "TC10")),
}


def usb_vid(resource):
    """Vendor id out of a VISA resource string, WITHOUT opening the device.
    pyvisa-py writes it in decimal, NI-VISA in hex; int(x, 0) reads both."""
    parts = resource.split("::")
    if len(parts) < 2 or not parts[0].upper().startswith("USB"):
        return None
    try:
        return int(parts[1], 0)
    except ValueError:
        return None


def ask_usbtmc(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDWR)
        os.write(fd, b"*IDN?\n")
        return os.read(fd, 256).decode(errors="replace").strip()
    except Exception as exc:
        return f"<{type(exc).__name__}: {exc}>"
    finally:
        if fd is not None:
            os.close(fd)


def main():
    found = {}      # env var -> resource string
    rows = []       # (resource, idn)
    claimed = set()  # vendor ids already answering on a char device

    # 1. Kernel usbtmc char devices FIRST, and if one answers we do NOT go on to
    #    probe that same instrument over VISA -- opening it with pyvisa/libusb
    #    DETACHES the kernel driver, and the /dev/usbtmc* node you were about to
    #    paste into .env disappears from under you.
    for path in sorted(glob.glob("/dev/usbtmc*")):
        idn = ask_usbtmc(path)
        rows.append((path, idn))
        up = idn.upper()
        for vid, (_label, var, markers) in KNOWN.items():
            if any(m in up for m in markers):
                found.setdefault(var, path)
                claimed.add(vid)

    # 2. VISA. Only KNOWN vendor ids are opened: reading a stranger's *IDN?
    #    disturbs whoever owns it, which is how instruments start dropping out.
    try:
        import pyvisa
        rm = pyvisa.ResourceManager("@py")
        for res in rm.list_resources():
            vid = usb_vid(res)
            if vid is None:
                rows.append((res, "<not USB -- not probed>"))
                continue
            if vid in claimed:
                rows.append((res, "<same instrument as the /dev/usbtmc* above; "
                                  "not probed -- that would detach the kernel driver>"))
                continue
            if vid not in KNOWN:
                rows.append((res, f"<unknown vendor {vid:#06x} -- not probed>"))
                continue
            dev = None
            try:
                dev = rm.open_resource(res, open_timeout=4000)
                dev.timeout = 4000
                idn = dev.query("*IDN?").strip()
            except Exception as exc:
                idn = f"<{type(exc).__name__}: {exc}>"
            finally:
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
            rows.append((res, idn))
            if not idn.startswith("<"):
                found.setdefault(KNOWN[vid][1], res)
    except ImportError:
        print("pyvisa is not installed here -- run this inside the container, or\n"
              "  pip install pyvisa pyvisa-py pyusb\n", file=sys.stderr)

    if not rows:
        print("No instruments found. Check they are powered on and plugged in,\n"
              "and that ros2_ws/udev/99-scopio-instruments.rules is installed.")
        return 1

    width = max(len(r) for r, _ in rows)
    print()
    for res, idn in rows:
        print(f"  {res:<{width}}  {idn}")

    print("\n" + "-" * 70)
    print("Paste into ros2_ws/.env (then: docker compose up -d):\n")
    for vid, (label, var, _markers) in KNOWN.items():
        res = found.get(var)
        if res:
            print(f"  # {label}")
            print(f"  {var}={res}\n")
        else:
            print(f"  # {label}: NOT FOUND -- not plugged in, or busy (stop the backend)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
