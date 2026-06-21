"""Shared helpers for the standalone galvo bring-up tests.

These tests are deliberately independent of the microscope UI/server -- they
only need pyvisa and the Galvo driver. Import this for resource discovery and
to make ``microscope/galvo.py`` importable.
"""

import os
import sys

# Make `microscope/galvo.py` importable from these sibling scripts.
_MICROSCOPE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "microscope")
if _MICROSCOPE_DIR not in sys.path:
    sys.path.insert(0, _MICROSCOPE_DIR)


def resolve_resource(verbose=True):
    """Return the VISA resource string for the galvo's AWG.

    Order of preference: the GALVO_RESOURCE env var, else the first USB
    instrument found. Returns None if nothing is found.
    """
    import pyvisa

    rm = pyvisa.ResourceManager()
    resources = rm.list_resources()
    if verbose:
        print("VISA resources:", resources or "(none found)")

    env = os.environ.get("GALVO_RESOURCE")
    if env:
        return env

    usb = [r for r in resources if r.upper().startswith("USB")]
    if usb:
        if verbose and len(usb) > 1:
            print(f"Multiple USB instruments; using {usb[0]}. "
                  f"Set GALVO_RESOURCE to pick another.")
        return usb[0]
    return None
