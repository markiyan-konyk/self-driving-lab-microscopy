#!/usr/bin/env python3
"""
buffer_probe.py — Find the largest arb-data block the DG1022Z will accept
in a single USBTMC write on this Raspberry Pi + pyvisa-py setup.

How it works:
  It uploads dummy DAC blocks of INCREASING size using
      :SOUR<CH>:DATA:DAC VOLATILE,#<block>
  The plain DAC command OVERWRITES volatile memory each time, so every
  test is independent (nothing accumulates). After each write it queries
  :SYST:ERR? — a write can pass at the USB level but still be rejected by
  the instrument's SCPI parser, and that counts as a failure too.

  The sweep starts very small and grows in gentle steps (never a big
  jump), printing each attempt before sending it. It STOPS at the first
  failure — USB timeout OR instrument error. The last size that PASSED is
  your safe per-command ceiling.

What to do with the result:
  Chunk your real waveform to a size safely BELOW the ceiling and assemble
  it on the instrument with the CON/END mechanism:
      all chunks but the last:  :SOUR1:DATA:DAC16 VOLATILE,CON,#<block>
      the final chunk:          :SOUR1:DATA:DAC16 VOLATILE,END,#<block>
  (END makes the instrument switch to arbitrary-waveform output.)

Note: the number that matters is BYTES, not points — binary is 2 bytes
per point, plus ~32 bytes of command/header overhead. 8 points is the
instrument's minimum valid waveform, so the sweep starts there (smaller
blocks are rejected for being too short, not for buffer reasons).
"""

import sys
import time
import numpy as np
import pyvisa

# ============================================================
# ★★★ CONFIG ★★★
# ============================================================
RESOURCE   = ""      # e.g. "USB0::0x1AB1::0x0642::DG1ZA278M01038::INSTR"
                     # leave "" to auto-pick the first USB instrument
CH         = 1
TIMEOUT_MS = 5000    # per-write; an over-large block STALLS, so keep this modest
SETTLE_S   = 0.05    # pause between tests
# ============================================================


def build_sizes():
    """Increasing point counts: tiny at first, gently growing steps, never
    a big jump. Edit the (cap, step) tiers to probe finer or coarser."""
    sizes = [8]
    tiers = [(128, 8), (512, 32), (2048, 64), (8192, 256), (16384, 512)]
    n = 8
    for cap, step in tiers:
        while n < cap:
            n += step
            if n <= 16384:
                sizes.append(n)
    return sizes


def err_code(resp):
    """Parse the leading integer of a :SYST:ERR? response ('0,"No error"')."""
    try:
        return int(resp.split(",")[0])
    except Exception:
        return -9999  # unparseable -> treat as an error


def pick_resource(rm):
    if RESOURCE:
        return RESOURCE
    usb = [r for r in rm.list_resources() if r.upper().startswith("USB")]
    if not usb:
        print("No USB instrument found. Set RESOURCE manually.")
        sys.exit(1)
    return usb[0]


def main():
    rm = pyvisa.ResourceManager("@py")
    res = pick_resource(rm)
    print(f"Opening {res}")
    dev = rm.open_resource(res)
    dev.timeout = TIMEOUT_MS
    dev.chunk_size = 20 * 1024 * 1024   # don't let pyvisa pre-split the write:
                                        # we want the device's true single-write limit
    try:
        dev.clear()                     # reset any endpoint left stalled by a prior run
    except Exception:
        pass
    print("IDN:", dev.query("*IDN?").strip())
    dev.write("*CLS")                   # clear the instrument's error queue

    header = f":SOUR{CH}:DATA:DAC VOLATILE,"
    last_ok = None

    for n in build_sizes():
        payload = 2 * n                 # uint16 -> 2 bytes/point
        data = np.linspace(0, 16383, n).astype(np.uint16)   # dummy ramp; content irrelevant
        preview = ", ".join(str(v) for v in data[:4])
        print(f"\n-> Trying {n:>6} pts | {payload:>6} data bytes "
              f"(~{payload + 32} total) | first vals: [{preview}, ...]")

        try:
            dev.write_binary_values(header, data,
                                    datatype="H", is_big_endian=False)
            resp = dev.query(":SYST:ERR?").strip()
        except Exception as e:
            print(f"   USB WRITE FAILED: {e}")
            print(f"\n==> LIMIT reached: {n} pts ({payload} bytes) would not transfer.")
            break

        code = err_code(resp)
        if code != 0:
            print(f"   INSTRUMENT REJECTED IT: :SYST:ERR? -> {resp}")
            print(f"\n==> LIMIT reached: instrument refused {n} pts ({payload} bytes).")
            break

        print(f"   OK  (:SYST:ERR? -> {resp})")
        last_ok = (n, payload)
        time.sleep(SETTLE_S)

    print("\n" + "=" * 56)
    if last_ok:
        n, payload = last_ok
        safe = int(n * 0.8)
        print(f"Largest block that PASSED : {n} pts  ({payload} data bytes)")
        print(f"Suggested safe chunk size : ~{safe} pts  (20% margin)")
        print("Assemble your full waveform with :DATA:DAC16 VOLATILE,CON/END.")
    else:
        print("Even the smallest block failed — check the error printed above.")
    print("=" * 56)

    try:
        dev.write(f":OUTP{CH} OFF")
    except Exception:
        pass
    dev.close()


if __name__ == "__main__":
    sys.exit(main())