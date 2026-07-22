"""
galvo_circle_test.py - Find the DC-command rate ceiling of a Rigol DG1022Z driving galvos.

A "pointer" travels continuously around a circle whose position is a function of the
WALL CLOCK, not of the loop counter:

    theta(t) = 2*pi * (t - t0) / period
    x = cx + radius*cos(theta)      -> CH1 (X galvo)  DC offset
    y = cy + radius*sin(theta)      -> CH2 (Y galvo)  DC offset

We only *sample* that circle every 1/rate seconds and push the position as a DC command.
At rate=1 Hz with period=4 s you see 4 points per loop (your "4 dots"); crank the rate up
and the circle fills in. The script runs a ladder of rates, and for each one reports the
rate it ACTUALLY achieved, the timing jitter, any instrument errors, and any auto-reconnects.
Where "achieved" stops tracking "target" (or errors appear) is the wavegen's ceiling.

    pip install pyvisa pyvisa-py numpy
    python3 galvo_circle_test.py --resource TCPIP::192.168.1.10::INSTR --radius 1.0

Run it in the same folder as wavegen.py.

  !!! SAFETY: --radius and --max-volts are VOLTS into your galvo driver. Set them to your
      driver's spec BEFORE running. Defaults are deliberately small (1 V). Every command is
      clamped to +/-max-volts so a bad arg can't slam the mirror to full scale. !!!
"""

import time
import argparse

import numpy as np

from galvo import WaveGen

X_CH, Y_CH = 1, 2          # CH1 = X galvo, CH2 = Y galvo


def setup(gen, args):
    """Put both channels in DC mode once, set load, center the pointer, enable outputs."""
    for ch in (X_CH, Y_CH):
        gen.set_function(gen.DC, ch)               # DC mode ONCE; then we only nudge the offset
        # High-Z so the volts you command are the volts that appear at a high-impedance galvo
        # input. If the load were left at 50 ohm, the real output would be ~2x what you set.
        gen.set_output_load(ch, gen.HIGH_Z)
    gen.set_offset(args.cx, X_CH)                  # start at circle center
    gen.set_offset(args.cy, Y_CH)
    for ch in (X_CH, Y_CH):
        gen.output(True, ch)
    errs = gen.get_errors()                        # clear the slate before measuring
    if errs:
        print("  (cleared pre-existing errors:", errs, ")")


def move_to(gen, x, y, verify, use_apply):
    """One position update = two writes (X then Y). This is the thing being rate-limited."""
    if use_apply:                                  # heavier fallback if :VOLT:OFFS won't move DC live
        gen.apply_dc(x, X_CH); gen.apply_dc(y, Y_CH)
    else:                                          # lighter: just change the DC level
        gen.set_offset(x, X_CH); gen.set_offset(y, Y_CH)
    if verify:
        gen.opc()                                  # block until the instrument reports done (honest rate)


def run_stage(gen, args, rate, t0):
    """Stream the circle for args.dwell seconds at a target `rate`; return measured stats."""
    clamp = lambda v: float(np.clip(v, -args.max_volts, args.max_volts))
    dt = 1.0 / rate
    recon_before = gen.reconnects
    gen.get_errors()                               # clear queue so post-stage errors are from THIS stage

    stage_start = time.perf_counter()
    sent = []                                      # timestamps of each completed update
    k = 0
    while time.perf_counter() - stage_start < args.dwell:
        now = time.perf_counter()
        theta = 2.0 * np.pi * ((now - t0) / args.period)      # position from the wall clock
        pos = np.array([args.cx, args.cy]) + args.radius * np.array([np.cos(theta), np.sin(theta)])
        move_to(gen, clamp(pos[0]), clamp(pos[1]), args.verify, args.use_apply)
        sent.append(time.perf_counter())
        k += 1
        # absolute-time scheduling: when we can't keep up, the sleep goes negative and we
        # free-run, so `achieved` plateaus at the true throughput ceiling instead of drifting.
        slack = (stage_start + k * dt) - time.perf_counter()
        if slack > 0:
            time.sleep(slack)

    errs = gen.get_errors()
    recon = gen.reconnects - recon_before
    if len(sent) >= 2:
        intervals = np.diff(sent)                  # numpy for the timing statistics
        achieved = (len(sent) - 1) / (sent[-1] - sent[0])
        mean_ms, jit_ms, max_ms = intervals.mean() * 1e3, intervals.std() * 1e3, intervals.max() * 1e3
    else:
        achieved = len(sent) / args.dwell
        mean_ms = jit_ms = max_ms = float("nan")

    return dict(target=rate, updates=len(sent), achieved=achieved,
                mean_ms=mean_ms, jit_ms=jit_ms, max_ms=max_ms, errs=errs, recon=recon)


def verdict(s):
    reasons = []
    if s["achieved"] < 0.9 * s["target"]:
        reasons.append("can't keep up")           # host/link/instrument throughput bound
    if s["errs"]:
        reasons.append(f"{len(s['errs'])} err")   # instrument complained (e.g. -363 buffer overrun)
    if s["recon"]:
        reasons.append(f"{s['recon']} reconnect")  # link had to auto-recover -> unstable here
    return "ok" if not reasons else "FAIL: " + ", ".join(reasons)


def main():
    p = argparse.ArgumentParser(description="Ramp DC update rate to find the DG1022Z galvo ceiling.")
    p.add_argument("--resource", default="TCPIP::192.168.1.10::INSTR", help="VISA resource string")
    p.add_argument("--radius", type=float, default=1.0, help="circle radius in VOLTS (match galvo spec)")
    p.add_argument("--cx", type=float, default=0.0, help="center X offset, volts")
    p.add_argument("--cy", type=float, default=0.0, help="center Y offset, volts")
    p.add_argument("--max-volts", type=float, default=5.0, help="absolute safety clamp per axis, volts")
    p.add_argument("--period", type=float, default=4.0, help="seconds per full circle")
    p.add_argument("--dwell", type=float, default=8.0, help="seconds to hold each rate")
    p.add_argument("--rates", default="1,2,5,10,20,30,50,75,100",
                   help="comma-separated target update rates in Hz")
    p.add_argument("--verify", action="store_true", help="*OPC? after each update (honest settled rate)")
    p.add_argument("--use-apply", action="store_true",
                   help="use APPL:DC per update instead of VOLT:OFFS (if offset won't move DC live)")
    p.add_argument("--stop-on-fail", action="store_true", help="stop at the first failing rate")
    args = p.parse_args()

    rates = [float(r) for r in args.rates.split(",") if r.strip()]

    print(f"Connecting to {args.resource} ...")
    with WaveGen(args.resource) as gen:
        print("IDN:", gen.idn())
        print(f"period={args.period}s  radius={args.radius}V  clamp=+/-{args.max_volts}V  "
              f"dwell={args.dwell}s/stage  mode={'APPL:DC' if args.use_apply else 'VOLT:OFFS'}"
              f"{'  +OPC verify' if args.verify else ''}\n")
        setup(gen, args)

        header = f"{'target Hz':>9} {'updates':>8} {'got Hz':>8} {'cmd/s':>7} {'dt ms':>7} {'jit ms':>7} {'max ms':>7}  verdict"
        print(header)
        print("-" * len(header))

        t0 = time.perf_counter()                   # circle clock origin; shared across all stages
        try:
            for rate in rates:
                s = run_stage(gen, args, rate, t0)
                v = verdict(s)
                print(f"{s['target']:>9.0f} {s['updates']:>8d} {s['achieved']:>8.1f} "
                      f"{s['achieved']*2:>7.0f} {s['mean_ms']:>7.1f} {s['jit_ms']:>7.1f} "
                      f"{s['max_ms']:>7.1f}  {v}")
                if s["errs"]:
                    for code, msg in s["errs"]:
                        print(f"            -> [{code}] {msg}")
                if args.stop_on_fail and v != "ok":
                    print("\nStopping at first failing rate.")
                    break
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            # Recenter the mirror, then drop the outputs. Leaving a galvo parked mid-swing is rude.
            for ch in (X_CH, Y_CH):
                try:
                    gen.set_offset(0.0, ch)
                except Exception:
                    pass
            time.sleep(0.1)
            for ch in (X_CH, Y_CH):
                try:
                    gen.output(False, ch)
                except Exception:
                    pass
            print("Recentered, outputs off. Total auto-reconnects:", gen.reconnects)


if __name__ == "__main__":
    main()