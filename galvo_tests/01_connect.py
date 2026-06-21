"""Step 1 - Can we even talk to the galvo's waveform generator?

Lists the VISA resources, opens the Rigol DG1022Z, and prints *IDN?. Run this
first; if it fails, nothing downstream will work. No motion is commanded.

    python galvo_tests/01_connect.py
    GALVO_RESOURCE="USB0::0x1AB1::0x0642::DG1ZA...::INSTR" python galvo_tests/01_connect.py
"""

import sys

from _common import resolve_resource


def main():
    res = resolve_resource()
    if not res:
        print("No galvo resource found. Plug in the DG1022Z (USB) or set "
              "GALVO_RESOURCE to its VISA address.")
        return 1

    print(f"Opening {res} ...")
    import pyvisa
    rm = pyvisa.ResourceManager()
    awg = rm.open_resource(res)
    awg.timeout = 5000
    try:
        idn = awg.query("*IDN?").strip()
        print("IDN:", idn)
        if "DG1" not in idn.upper():
            print("WARNING: this does not look like a DG1022Z. Double-check the "
                  "resource address.")
        print("OK - connection works.")
        return 0
    finally:
        awg.close()


if __name__ == "__main__":
    sys.exit(main())
