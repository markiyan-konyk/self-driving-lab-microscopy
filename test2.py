#!/usr/bin/env python3
"""
usbtmc_diagnose.py — Find out WHY the DG1022Z arb upload fails, because a
24-point "limit" is not a real buffer limit (48 bytes; USB moves data in
512-byte packets, and we already put ~14 KB on the wire earlier).

The old size-probe read :SYST:ERR? with NO pause after the write, and lumped
the write and that readback into one try/except. A DATA:DAC upload makes the
instrument reallocate the arb and switch output mode, which takes time — query
too soon and the READ times out, looking like a small-write failure.

This tests the two things that actually matter:
  * REPRODUCIBILITY — retry each size several times. Consistent failure at a
    size = real limit. Random failure = transport instability.
  * PACING          — wait after each write before reading back, and report
    write-failures and readback-failures in SEPARATE columns.

How to read the result:
  * If the big sizes now pass once there's a pause -> the problem was pacing,
    not size. The real uploader just needs a settle + a retry.
  * If sizes still fail at random even small -> it's a pyvisa-py USBTMC
    transport issue: retry the whole upload on failure (with dev.clear()
    between tries), or switch to the python-usbtmc backend.
"""

import time
import numpy as np
import pyvisa

# ============================================================
# ★★★ CONFIG ★★★
# ============================================================
RESOURCE   = "USB0::0x1AB1::0x0642::DG1ZA278M01038::INSTR"  # "" to auto-pick
CH         = 1
TIMEOUT_MS = 4000
TEST_SIZES = [16, 32, 64, 128, 256, 512, 1024]   # points
REPEATS    = 6
SETTLE_AFTER_WRITE = 0.3     # KEY: let the instrument finish before reading back
SETTLE_BETWEEN     = 0.1
# ============================================================


def err_code(resp):
    try:
        return int(resp.split(",")[0])
    except Exception:
        return -9999


def recover(dev):
    """Reset the USBTMC endpoints after a timeout so one failure doesn't
    cascade into the next attempt."""
    try:
        dev.clear()
    except Exception:
        pass
    time.sleep(0.2)


def main():
    rm = pyvisa.ResourceManager("@py")
    res = RESOURCE or next(r for r in rm.list_resources()
                           if r.upper().startswith("USB"))
    print("Opening", res)
    dev = rm.open_resource(res)
    dev.timeout = TIMEOUT_MS
    recover(dev)
    print("IDN:", dev.query("*IDN?").strip())

    header = f":SOUR{CH}:DATA:DAC VOLATILE,"
    print(f"\n{'points':>7} {'bytes':>7} | {'writes':>9} {'readbacks':>10} "
          f"{'scpi_ok':>9}")
    print("-" * 52)

    for n in TEST_SIZES:
        data = np.linspace(0, 16383, n).astype(np.uint16)
        w_ok = r_ok = e_ok = 0
        for _ in range(REPEATS):
            # --- write path (binary block accepted over USB?) ---
            try:
                dev.write("*CLS")
                dev.write_binary_values(header, data,
                                        datatype="H", is_big_endian=False)
                w_ok += 1
            except Exception:
                recover(dev)
                time.sleep(SETTLE_BETWEEN)
                continue

            time.sleep(SETTLE_AFTER_WRITE)     # <-- the pacing fix being tested

            # --- readback path (did the instrument answer, and was it happy?) ---
            try:
                resp = dev.query(":SYST:ERR?").strip()
                r_ok += 1
                if err_code(resp) == 0:
                    e_ok += 1
            except Exception:
                recover(dev)
            time.sleep(SETTLE_BETWEEN)

        print(f"{n:>7} {2*n:>7} | {w_ok:>4}/{REPEATS:<4} {r_ok:>5}/{REPEATS:<4} "
              f"{e_ok:>4}/{REPEATS:<4}")

    try:
        dev.write(f":OUTP{CH} OFF")
    except Exception:
        pass
    dev.close()

    print("\nColumns:")
    print("  writes    = binary block accepted over USB without timeout")
    print("  readbacks = :SYST:ERR? actually answered")
    print("  scpi_ok   = of those, how many reported 0 / No error")
    print("All three at or near REPEATS for a size = that size is reliable.")


if __name__ == "__main__":
    main()