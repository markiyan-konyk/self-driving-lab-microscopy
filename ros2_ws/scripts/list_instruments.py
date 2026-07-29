#!/usr/bin/env python3
"""Debug tool: list the VISA instruments this machine can see.

The nodes auto-discover by vendor id and do not need this. Use it when an
instrument is not showing up, or to get an address for ros2_ws/.env.

    docker compose down            # a running node holds its instrument open
    python3 scripts/list_instruments.py
"""

import pyvisa

KNOWN = {0x1AB1: "GALVO_RESOURCE", 0x1A45: "TCLAB_RESOURCE"}


def usb_vid(resource):
    parts = resource.split("::")
    if len(parts) < 2 or not parts[0].upper().startswith("USB"):
        return None
    try:
        return int(parts[1], 0)
    except ValueError:
        return None


rm = pyvisa.ResourceManager("@py")
for res in rm.list_resources():
    vid = usb_vid(res)
    if vid not in KNOWN:
        print(f"{res}  <unknown vendor, not probed>")
        continue
    try:
        dev = rm.open_resource(res, open_timeout=4000)
        dev.timeout = 4000
        idn = dev.query("*IDN?").strip()
        dev.close()
    except Exception as exc:
        idn = f"<{type(exc).__name__}: {exc}>"
    print(f"{res}  {idn}")
    print(f"    {KNOWN[vid]}={res}")
