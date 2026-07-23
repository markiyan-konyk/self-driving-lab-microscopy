#!/usr/bin/env python3
"""instrument_scan.py - why can't VISA see my instrument?

`list_resources()` returning nothing is not one bug, it is five, and they need
different fixes. This looks UNDERNEATH pyvisa at what the machine actually has:

  1. what Python/VISA libraries are installed (a missing pyserial silently
     hides EVERY serial instrument from list_resources)
  2. every USB device the kernel enumerated, with the interface CLASS of each --
     the thing that decides whether pyvisa can ever see it:
         0xFE/0x03  USBTMC ...... appears as USB0::0x...::INSTR   (the Rigol AWG)
         0x02/0x0A  CDC serial .. appears as ASRL/dev/ttyACM0::INSTR, NOT "USB..."
         0xFF       vendor ...... usually an FTDI/CP210x -> also ASRL
     A controller that speaks CDC will NEVER show up as a USB:: resource, no
     matter how correct the rest of your setup is.
  3. which kernel driver claimed each interface (cdc_acm / ftdi_sio / usbtmc)
  4. the serial ports, mapped back to their USB device
  5. what pyvisa itself reports, for comparison

With --probe it then asks every candidate "*IDN?" -- over VISA, and over raw
serial at the usual baud rates -- and prints the exact line to paste into
ros2_ws/.env for whatever answers.

    python instrument_scan.py                 # safe: reads descriptors only
    python instrument_scan.py --probe         # also talks to each candidate
    python instrument_scan.py --probe --skip /dev/ttyACM0      # e.g. the stage

Run it BOTH on the Pi host and inside the container
(`docker compose exec scopio python3 /workspace/instrument_scan.py`): a device
the host sees and the container doesn't is a passthrough/permissions problem,
not an instrument problem.
"""

import argparse
import glob
import os
import sys

# USB interface classes worth naming. (class, subclass) -> what it means for us.
USB_CLASSES = {
    (0xFE, 0x03): ("USBTMC", "pyvisa sees this as USB0::0x....::INSTR"),
    (0x02, None): ("CDC control", "virtual COM port -> ASRL resource, never USB::"),
    (0x0A, None): ("CDC data", "virtual COM port -> ASRL resource, never USB::"),
    (0xFF, None): ("vendor-specific", "often FTDI/CP210x -> ASRL resource"),
    (0x03, None): ("HID", "not reachable through VISA at all"),
    (0x08, None): ("mass storage", "not an instrument interface"),
    (0x09, None): ("hub", ""),
    (0x0E, None): ("video", "camera"),
    (0x01, None): ("audio", ""),
}

BAUD_CANDIDATES = (115200, 9600, 38400, 57600, 19200, 230400)

# Overridable so the parser can be exercised against a synthetic tree.
SYSFS_USB = "/sys/bus/usb/devices"


def rd(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def node_access(node):
    """Can THIS user open the device node? libusb must, to read the descriptors
    pyvisa needs -- and a root-only node is invisible, not an error."""
    try:
        st = os.stat(node)
    except OSError as exc:
        return f"(cannot stat: {exc.strerror})"
    try:
        import grp
        import pwd
        owner = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    except (ImportError, KeyError, AttributeError):   # non-POSIX, or unnamed ids
        owner = f"{st.st_uid}:{st.st_gid}"
    mode = oct(st.st_mode & 0o777)[2:]
    ok = os.access(node, os.R_OK | os.W_OK)
    return f"{mode} {owner}  {'read/write OK' if ok else 'NOT WRITABLE BY YOU <<<<'}"


def classify(cls, sub):
    for key in ((cls, sub), (cls, None)):
        if key in USB_CLASSES:
            return USB_CLASSES[key]
    return (f"class 0x{cls:02X}", "")


# --------------------------------------------------------------------------- #
#  1. libraries
# --------------------------------------------------------------------------- #
def section_libraries():
    print("=" * 78)
    print("1. LIBRARIES")
    print("=" * 78)
    have = {}
    for mod, why in (("pyvisa", "the VISA front-end"),
                     ("pyvisa_py", "the pure-python backend ('@py')"),
                     ("usb", "pyusb -- REQUIRED for USBTMC instruments"),
                     ("serial", "pyserial -- REQUIRED for serial/CDC instruments"),
                     ("usb1", "optional alternative libusb binding")):
        try:
            m = __import__(mod)
            have[mod] = True
            print(f"  ok       {mod:<10} {getattr(m, '__version__', ''):<10} {why}")
        except ImportError:
            have[mod] = False
            print(f"  MISSING  {mod:<10} {'':<10} {why}")
    if not have.get("serial"):
        print("\n  !! pyserial is missing: list_resources() cannot report ANY serial")
        print("     instrument. If your controller is a virtual COM port, this alone")
        print("     explains 'VISA cannot find it'.  ->  pip install pyserial")
    if not have.get("usb"):
        print("\n  !! pyusb is missing: no USBTMC instrument can be found either.")
    return have


# --------------------------------------------------------------------------- #
#  2/3. USB devices, straight from the kernel
# --------------------------------------------------------------------------- #
def usb_from_sysfs():
    """Linux: read /sys/bus/usb/devices. No root, no pyusb, works in-container."""
    devices = []
    for path in sorted(glob.glob(os.path.join(SYSFS_USB, "*"))):
        name = os.path.basename(path)
        if ":" in name or name.startswith("usb"):
            continue                       # interface entry / root hub
        vid, pid = rd(f"{path}/idVendor"), rd(f"{path}/idProduct")
        if not vid:
            continue
        busnum, devnum = rd(f"{path}/busnum"), rd(f"{path}/devnum")
        interfaces = []
        for ipath in sorted(glob.glob(f"{path}:*")):
            try:
                cls = int(rd(f"{ipath}/bInterfaceClass", "-1"), 16)
                sub = int(rd(f"{ipath}/bInterfaceSubClass", "0"), 16)
            except ValueError:
                continue
            driver = ""
            link = f"{ipath}/driver"
            if os.path.islink(link):
                driver = os.path.basename(os.readlink(link))
            interfaces.append({"class": cls, "subclass": sub, "driver": driver,
                               "name": os.path.basename(ipath)})
        devices.append({
            "bus_id": name, "vid": vid, "pid": pid,
            "product": rd(f"{path}/product"),
            "manufacturer": rd(f"{path}/manufacturer"),
            "serial": rd(f"{path}/serial"),
            "interfaces": interfaces,
            # The node libusb must open. Its permissions are the usual reason a
            # perfectly good USBTMC instrument is invisible to a non-root user.
            "node": (f"/dev/bus/usb/{int(busnum):03d}/{int(devnum):03d}"
                     if busnum.isdigit() and devnum.isdigit() else ""),
        })
    return devices


def usb_from_pyusb():
    """Fallback for Windows/macOS (or a container without sysfs)."""
    try:
        import usb.core
        import usb.util
    except ImportError:
        return []
    out = []
    try:
        found = list(usb.core.find(find_all=True))
    except Exception as exc:
        print(f"  ! pyusb enumeration failed: {exc}")
        return []
    for dev in found:
        def s(getter):
            try:
                return getter() or ""
            except Exception:
                return "(unreadable -- driver/permissions)"
        interfaces = []
        try:
            for cfg in dev:
                for intf in cfg:
                    interfaces.append({"class": intf.bInterfaceClass,
                                       "subclass": intf.bInterfaceSubClass,
                                       "driver": "", "name": f"{intf.bInterfaceNumber}"})
        except Exception:
            pass
        out.append({
            "bus_id": f"bus{dev.bus}.dev{dev.address}",
            "vid": f"{dev.idVendor:04x}", "pid": f"{dev.idProduct:04x}",
            "product": s(lambda: usb.util.get_string(dev, dev.iProduct)),
            "manufacturer": s(lambda: usb.util.get_string(dev, dev.iManufacturer)),
            "serial": s(lambda: usb.util.get_string(dev, dev.iSerialNumber)),
            "interfaces": interfaces,
        })
    return out


def section_usb():
    print()
    print("=" * 78)
    print("2. USB DEVICES (what the kernel enumerated)")
    print("=" * 78)
    devices, source = usb_from_sysfs(), "kernel sysfs"
    if not devices:
        devices, source = usb_from_pyusb(), "pyusb/libusb"
    print(f"  (source: {source})")

    if not devices:
        if sys.platform.startswith("linux"):
            print("  Nothing enumerated. If the instrument is plugged in and powered,")
            print("  this is a cable/hub/power problem -- or, in a container, missing")
            print("  device passthrough. Run this on the host to compare.")
        else:
            print("  pyusb enumerated nothing. On Windows/macOS that is USUALLY NOT a")
            print("  fault: pyusb only sees devices with a libusb-compatible driver")
            print("  bound, while the vendor's own app uses a COM port or the vendor")
            print("  driver. Section 3 (serial ports) is the meaningful one here --")
            print("  and run this on the Pi for the full picture.")
        return [], []

    tmc, serialish = [], []
    for d in devices:
        kinds = []
        for i in d["interfaces"]:
            label, note = classify(i["class"], i["subclass"])
            kinds.append((label, note, i["driver"]))
        is_hub = all(k[0] == "hub" for k in kinds) if kinds else False
        if is_hub:
            continue
        title = " / ".join(x for x in (d["manufacturer"], d["product"]) if x) or "(no strings)"
        print(f"\n  {d['vid']}:{d['pid']}  {title}")
        if d["serial"]:
            print(f"      serial#   {d['serial']}")
        print(f"      sysfs/bus {d['bus_id']}")
        if d.get("node"):
            print(f"      node      {d['node']}  {node_access(d['node'])}")
        for (label, note, driver) in kinds:
            drv = f"driver={driver or 'none'}"
            print(f"      iface     {label:<16} {drv:<18} {note}")
        if any(k[0] == "USBTMC" for k in kinds):
            tmc.append(d)
        if any(k[0].startswith("CDC") or k[0] == "vendor-specific" for k in kinds):
            serialish.append(d)
    return tmc, serialish


# --------------------------------------------------------------------------- #
#  4. serial ports
# --------------------------------------------------------------------------- #
def section_serial():
    print()
    print("=" * 78)
    print("3. SERIAL PORTS")
    print("=" * 78)
    try:
        from serial.tools import list_ports
    except ImportError:
        print("  pyserial not installed -- cannot list serial ports, and pyvisa")
        print("  cannot use them either.   ->   pip install pyserial")
        return []
    ports = list(list_ports.comports())
    if not ports:
        print("  (none)")
        if os.name != "nt":
            leftovers = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
            if leftovers:
                print(f"  ...but these device nodes exist: {leftovers}")
                print("  (pyserial did not enumerate them -- check permissions)")
    for p in ports:
        vidpid = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None else "n/a"
        print(f"  {p.device:<20} {vidpid:<12} {p.description}")
        if p.serial_number:
            print(f"  {'':<20} serial# {p.serial_number}")
    return [p.device for p in ports]


# --------------------------------------------------------------------------- #
#  5. what pyvisa reports
# --------------------------------------------------------------------------- #
def section_visa():
    print()
    print("=" * 78)
    print("4. PYVISA list_resources()")
    print("=" * 78)
    try:
        import pyvisa
    except ImportError:
        print("  pyvisa not installed.")
        return []
    resources = []
    for backend in ("@py", ""):
        label = backend or "(default/IVI)"
        try:
            rm = pyvisa.ResourceManager(backend)
            found = list(rm.list_resources())
            print(f"  {label:<14} {found or '(nothing)'}")
            resources.extend(r for r in found if r not in resources)
        except Exception as exc:
            print(f"  {label:<14} unavailable: {exc}")
    return resources


# --------------------------------------------------------------------------- #
#  6. probe
# --------------------------------------------------------------------------- #
def probe_visa(resource, timeout_ms=2000):
    import pyvisa
    rm = pyvisa.ResourceManager("@py")
    inst = None
    try:
        inst = rm.open_resource(resource)
        inst.timeout = timeout_ms
        inst.read_termination = "\n"
        inst.write_termination = "\n"
        return (inst.query("*IDN?") or "").strip()
    finally:
        try:
            if inst is not None:
                inst.close()
        except Exception:
            pass


def probe_serial(port, baud, timeout_s=1.0):
    import serial
    with serial.Serial(port, baud, timeout=timeout_s, write_timeout=timeout_s) as ser:
        ser.reset_input_buffer()
        ser.write(b"*IDN?\n")
        ser.flush()
        reply = ser.readline().decode("ascii", "replace").strip()
        if not reply:                      # some boxes want CRLF
            ser.write(b"*IDN?\r\n")
            ser.flush()
            reply = ser.readline().decode("ascii", "replace").strip()
        return reply


def section_deep(devices):
    """Replay pyvisa-py's USB enumeration STEP BY STEP for each USBTMC device.

    `list_resources()` is all-or-nothing: a device that fails any step just
    isn't there, with no error. pyvisa-py's own path is

        find_tmc_devices()            <- filters on the interface descriptor
          -> usb.core.find(find_all)
          -> find_interfaces(0xFE/3)  <- needs the CONFIG descriptor
        dev.serial_number             <- needs the device to be OPENED

    so we run each step separately and report exactly which one loses your
    instrument, and with which errno. Nothing here writes to the instrument;
    if a kernel driver has to be detached to test claiming, it is put back.
    """
    print()
    print("=" * 78)
    print("6. DEEP: pyvisa-py's own enumeration, one step at a time")
    print("=" * 78)
    try:
        import usb.core
        import usb.util
    except ImportError:
        print("  pyusb not installed -- nothing to test.")
        return

    # What pyvisa-py's list_resources() actually iterates over.
    try:
        from pyvisa_py.protocols import usbtmc as pvp_usbtmc
        from pyvisa_py.protocols import usbutil as pvp_usbutil
        tmc_devs = list(pvp_usbtmc.find_tmc_devices())
        print(f"  pyvisa_py.find_tmc_devices() -> {len(tmc_devs)} device(s):")
        for d in tmc_devs:
            print(f"      {d.idVendor:04x}:{d.idProduct:04x}")
        print("  Any USBTMC device MISSING from that list is invisible to")
        print("  list_resources() no matter what else is right.\n")
    except Exception as exc:
        pvp_usbutil = None
        print(f"  (could not use pyvisa_py internals: {exc})\n")

    targets = [d for d in devices
               if any(i["class"] == 0xFE and i["subclass"] == 0x03 for i in d["interfaces"])]
    if not targets:
        print("  No USBTMC device in the sysfs scan to test.")
        return

    for d in targets:
        title = " / ".join(x for x in (d["manufacturer"], d["product"]) if x)
        print(f"  --- {d['vid']}:{d['pid']}  {title} ---")
        vid, pid = int(d["vid"], 16), int(d["pid"], 16)

        dev = None
        try:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            step("usb.core.find()", "found" if dev is not None else "NOT FOUND", dev is not None)
        except Exception as exc:
            step("usb.core.find()", f"{type(exc).__name__}: {exc}", False)
            continue
        if dev is None:
            continue

        # 1. config descriptor -> the USBTMC interface filter
        try:
            if pvp_usbutil is not None:
                intfs = pvp_usbutil.find_interfaces(dev, bInterfaceClass=0xFE,
                                                    bInterfaceSubClass=3)
            else:
                intfs = [i for cfg in dev for i in cfg
                         if i.bInterfaceClass == 0xFE and i.bInterfaceSubClass == 3]
            step("USBTMC interface filter", f"{len(intfs)} match(es)", bool(intfs),
                 "" if intfs else "the config descriptor could not be read -> "
                                  "find_tmc_devices() drops this device SILENTLY")
        except Exception as exc:
            step("USBTMC interface filter", f"{type(exc).__name__}: {exc}", False)

        # 2. serial number -> the first step that must OPEN the device
        try:
            serial = dev.serial_number
            step("read serial (opens device)", repr(serial), True)
        except Exception as exc:
            step("read serial (opens device)", f"{type(exc).__name__}: {exc}", False,
                 explain_usb_error(exc))

        # 3. kernel driver -- pyvisa-py detaches it when opening a session
        detached = False
        try:
            active = dev.is_kernel_driver_active(0)
            step("kernel driver on iface 0", "ACTIVE (usbtmc)" if active else "none", True,
                 "pyvisa-py detaches this itself when it opens a session"
                 if active else "")
            if active:
                try:
                    dev.detach_kernel_driver(0)
                    detached = True
                    step("detach_kernel_driver(0)", "ok", True)
                except Exception as exc:
                    step("detach_kernel_driver(0)", f"{type(exc).__name__}: {exc}", False,
                         explain_usb_error(exc))
        except Exception as exc:
            step("kernel driver on iface 0", f"{type(exc).__name__}: {exc}", False,
                 explain_usb_error(exc))

        # 4. claim -- what a real session needs
        try:
            usb.util.claim_interface(dev, 0)
            step("claim_interface(0)", "ok", True)
            usb.util.release_interface(dev, 0)
        except Exception as exc:
            step("claim_interface(0)", f"{type(exc).__name__}: {exc}", False,
                 explain_usb_error(exc))

        # leave the device exactly as found
        if detached:
            try:
                dev.attach_kernel_driver(0)
                step("kernel driver restored", "ok", True)
            except Exception as exc:
                step("kernel driver restored", f"{type(exc).__name__}: {exc}", False,
                     "re-plug the instrument to restore /dev/usbtmc*")
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass                      # never let cleanup hide the diagnosis
        print()


def step(label, result, ok, note=""):
    print(f"    {'ok  ' if ok else 'FAIL'}  {label:<28} {result}")
    if note:
        print(f"          -> {note}")


def explain_usb_error(exc):
    """Turn a libusb errno into the actual fix."""
    errno = getattr(exc, "errno", None)
    text = str(exc).lower()
    if errno == 13 or "access" in text or "permission" in text:
        return ("PERMISSIONS. libusb cannot open the device node as this user. "
                "Install the udev rule (ros2_ws/udev/99-scopio-instruments.rules) "
                "or re-run with sudo to confirm.")
    if errno == 16 or "busy" in text:
        return ("BUSY. Another process holds the interface -- the kernel usbtmc "
                "driver, or a running container/node that already opened it. "
                "Stop the SCOPIO stack and retry.")
    if errno == 19 or "no such device" in text:
        return "The device went away (re-enumerated?). Re-plug and retry."
    return ""


def section_probe(resources, ports, skip):
    print()
    print("=" * 78)
    print("5. PROBE  (asking each candidate '*IDN?')")
    print("=" * 78)
    hits = []

    for res in resources:
        if any(s in res for s in skip):
            print(f"  {res:<34} skipped")
            continue
        try:
            idn = probe_visa(res)
            if idn:
                print(f"  {res:<34} -> {idn}")
                hits.append((res, idn))
            else:
                print(f"  {res:<34} (opened, empty reply)")
        except Exception as exc:
            print(f"  {res:<34} {type(exc).__name__}: {str(exc)[:60]}")

    for port in ports:
        if port in skip:
            print(f"  {port:<34} skipped")
            continue
        for baud in BAUD_CANDIDATES:
            try:
                idn = probe_serial(port, baud)
            except Exception as exc:
                print(f"  {port} @ {baud:<7} {type(exc).__name__}: {str(exc)[:50]}")
                break                       # port unusable: no point trying faster
            if idn:
                print(f"  {port} @ {baud:<7} -> {idn}")
                hits.append((f"ASRL{port}::INSTR", idn, baud))
                break
            print(f"  {port} @ {baud:<7} (no reply)")
    return hits


# --------------------------------------------------------------------------- #
def env_var_for(idn):
    """Guess which .env line this instrument belongs on, from its *IDN? string."""
    low = idn.lower()
    if "rigol" in low or "dg10" in low:
        return "GALVO_RESOURCE"
    if "wavelength" in low or "tc lab" in low or "tclab" in low:
        return "TCLAB_RESOURCE"
    return None


def verdict(libs, tmc, serialish, ports, resources, hits, probed):
    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)

    for res, *rest in hits:
        idn = rest[0]
        baud = rest[1] if len(rest) > 1 else None
        var = env_var_for(idn)
        print(f"\n  ANSWERS: {res}")
        print(f"    {idn}")
        if baud:
            print(f"    NOTE: this is a SERIAL instrument at {baud} baud -- not USBTMC.")
            print(f"          pyvisa needs the baud rate set on the session; the driver")
            print(f"          currently only sets terminations (see the fix below).")
        if var:
            print(f"    ros2_ws/.env:  {var}={res}")

    if not probed:
        print("\n  Descriptors only (no --probe). Re-run with --probe to have each")
        print("  candidate identify itself.")

    if not tmc and not serialish and sys.platform.startswith("linux"):
        print("\n  No instrument-like USB device was enumerated at all. Before")
        print("  suspecting software: cable, power, front-panel interface setting")
        print("  (some controllers must be switched to remote/USB mode), and -- if")
        print("  you ran this in the container -- device passthrough.")
    elif not tmc and serialish:
        print("\n  There is NO USBTMC device besides (possibly) the AWG, but there IS")
        print("  a CDC/vendor USB device. That is the answer: your controller is a")
        print("  VIRTUAL COM PORT. It can never appear as 'USB0::...::INSTR'; it is")
        print("  an ASRL resource, and auto-discovery (which filters for names")
        print("  starting with 'USB') skips it by design.")
    if not libs.get("serial"):
        print("\n  Install pyserial and re-run -- serial instruments are invisible")
        print("  without it, both here and to list_resources().")
    print()


def main():
    p = argparse.ArgumentParser(description="Find out why VISA can't see an instrument.")
    p.add_argument("--probe", action="store_true",
                   help="ask each candidate '*IDN?' (opens the port; read-only query)")
    p.add_argument("--deep", action="store_true",
                   help="replay pyvisa-py's USB enumeration step by step to find "
                        "WHICH step drops an instrument (use when a USBTMC device "
                        "is enumerated by the kernel but missing from list_resources)")
    p.add_argument("--skip", action="append", default=[],
                   help="port/resource substring not to touch (repeatable), "
                        "e.g. the Sangaboard's /dev/ttyACM0")
    args = p.parse_args()

    print(f"platform: {sys.platform}   python: {sys.version.split()[0]}")
    print(f"cwd:      {os.getcwd()}")
    in_container = os.path.exists("/.dockerenv")
    print(f"context:  {'INSIDE a container' if in_container else 'host'}")

    libs = section_libraries()
    tmc, serialish = section_usb()
    ports = section_serial()
    resources = section_visa()
    if args.deep:
        section_deep(usb_from_sysfs() or usb_from_pyusb())
    hits = section_probe(resources, ports, args.skip) if args.probe else []
    verdict(libs, tmc, serialish, ports, resources, hits, args.probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
