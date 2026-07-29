#!/usr/bin/env python3
"""Replug a USB instrument WITHOUT touching it -- for when you are not at the bench.

A USBTMC device whose session was aborted mid-transfer leaves its endpoint
stalled: it still enumerates (lsusb sees it) but every query times out and then
returns "[Errno 32] Pipe error". Unplugging it clears that. So does a port
reset, which is the same ioctl the kernel issues on a replug -- and that you can
do over ssh.

    sudo python3 scripts/usb_reset.py                 # reset every known instrument
    sudo python3 scripts/usb_reset.py --vid 1a45      # just the TC10 LAB
    sudo python3 scripts/usb_reset.py --list          # show what is on the bus

Run it with the backend STOPPED (`docker compose down`); resetting a device out
from under a process that holds it open is how you wedge it again.

Needs root: the reset writes to /dev/bus/usb/BBB/DDD.

If this does not bring the instrument back, its firmware is wedged rather than
its USB link, and only a power cycle at the bench will fix it.
"""

import argparse
import fcntl
import glob
import os
import subprocess
import sys
import time

USBDEVFS_RESET = 0x5514        # _IO('U', 20), from <linux/usbdevice_fs.h>

KNOWN = {
    0x1A45: "Wavelength TC10 LAB (temperature)",
    0x1AB1: "Rigol DG1022Z (galvo)",
}


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def interfaces(name):
    """[(iface, driver)] for a device, e.g. [('1-1.1:1.0', 'usbtmc')].

    Driver "-" means NOBODY owns it. For a USBTMC instrument that is the
    tell-tale of a libusb client having detached the kernel driver: while it is
    unowned there is no /dev/usbtmc* node to talk to.
    """
    out = []
    for iface in sorted(glob.glob(f"/sys/bus/usb/devices/{name}:*")):
        link = os.path.join(iface, "driver")
        drv = os.path.basename(os.path.realpath(link)) if os.path.islink(link) else "-"
        out.append((os.path.basename(iface), drv))
    return out


def bind_usbtmc(name):
    """Hand the interfaces back to the kernel usbtmc driver. Returns a report."""
    try:
        subprocess.run(["modprobe", "usbtmc"], capture_output=True, timeout=10)
    except Exception:
        pass
    done = []
    for iface, drv in interfaces(name):
        if drv != "-":
            done.append(f"{iface} already bound to {drv}")
            continue
        try:
            with open("/sys/bus/usb/drivers/usbtmc/bind", "w") as f:
                f.write(iface)
            done.append(f"{iface} -> usbtmc")
        except OSError as exc:
            done.append(f"{iface} bind failed ({exc})")
    return done


def holders():
    """Best-effort: is the backend still running and holding the instrument?"""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    return [n for n in out.split() if "scopio" in n]


def devices():
    """[(vid, pid, busnum, devnum, sysfs_name, product)] for every USB device."""
    out = []
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        vid, pid = read(f"{d}/idVendor"), read(f"{d}/idProduct")
        bus, dev = read(f"{d}/busnum"), read(f"{d}/devnum")
        if not (vid and pid and bus and dev):
            continue          # interfaces and root hubs have no idVendor
        out.append((int(vid, 16), int(pid, 16), int(bus), int(dev),
                    os.path.basename(d), read(f"{d}/product")))
    return out


def reset(bus, dev):
    node = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    fd = os.open(node, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)
    return node


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vid", help="only this vendor id, e.g. 1a45")
    ap.add_argument("--list", action="store_true", help="list and exit")
    ap.add_argument("--no-bind", action="store_true",
                    help="skip handing the interface back to the kernel usbtmc driver")
    args = ap.parse_args()

    found = devices()
    if not found:
        print("No USB devices visible under /sys/bus/usb/devices.")
        return 1

    print("On the bus:")
    for vid, pid, bus, dev, name, product in found:
        tag = KNOWN.get(vid, product or "")
        print(f"  bus {bus:03d} dev {dev:03d}  {vid:04x}:{pid:04x}  {name:<10} {tag}")
        for iface, drv in interfaces(name):
            note = "  <-- UNOWNED: a libusb client detached the kernel driver" \
                   if drv == "-" else ""
            print(f"      {iface}  driver={drv}{note}")
    print(f"\n/dev/usbtmc*: {sorted(glob.glob('/dev/usbtmc*')) or '(none)'}")

    running = holders()
    if running:
        print(f"\n!! {', '.join(running)} still running. Those nodes reopen the")
        print("!! instrument every few seconds -- each attempt detaches the kernel")
        print("!! driver again, so nothing below will stick. Run `docker compose down`")
        print("!! first.")

    if args.list:
        return 0

    want = int(args.vid, 16) if args.vid else None
    targets = [d for d in found if (d[0] == want if want else d[0] in KNOWN)]
    if not targets:
        print("\nNothing to reset (no matching instrument on the bus).")
        return 1

    print()
    for vid, pid, bus, dev, name, _ in targets:
        label = KNOWN.get(vid, f"{vid:04x}:{pid:04x}")
        try:
            node = reset(bus, dev)
            print(f"  reset {label} at {node}")
        except PermissionError:
            print(f"  {label}: permission denied -- run with sudo")
            return 1
        except OSError as exc:
            print(f"  {label}: reset failed ({exc})")

    time.sleep(2)      # let the kernel re-enumerate and re-probe its drivers

    # A reset alone does not always get usbtmc back: once libusb has detached
    # it, the interface can come up unowned. Hand it over explicitly.
    if not args.no_bind and not glob.glob("/dev/usbtmc*"):
        print()
        for name in (t[4] for t in targets):
            for line in bind_usbtmc(name):
                print(f"  {line}")

    print(f"\n/dev/usbtmc*: {sorted(glob.glob('/dev/usbtmc*')) or '(none)'}")
    print("\nNow: python3 scripts/list_instruments.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
