import pyvisa, numpy as np, time

RESOURCE = "USB0::6833::1602::DG1ZA278M01038::0::INSTR"  # your VISA address
CH       = 1
LOAD     = "INF"        # "50" or "INF" (HighZ) — must match your setup for correct volts
V_START  = 0.0          # resting DC before the arb (V)
V_END    = 1.0          # final DC after the arb (V); set == V_START for Case A
SRATE    = 1e5          # Sa/s  -> arb duration = N_points / SRATE
TAIL_S   = 0.05         # flat hold at V_END, gives a handoff window (Case B only)

# --- your arbitrary trajectory in VOLTS: MUST start at V_START, end at V_END ---
# example: a smooth raised-cosine excursion from V_START to V_END
t = np.linspace(0, 1, 2000)
body = V_START + (V_END - V_START) * (1 - np.cos(np.pi * t)) / 2   # smooth S-curve
tail = np.full(int(TAIL_S * SRATE), V_END)                          # flat hold at end
volts = np.concatenate([body, tail])                               # <= 16384 points total
assert volts.size <= 16384, "DG1022Z volatile arb max is 16k points"

# --- map volts -> normalized [-1, 1] (1->max, -1->min per the manual) ---
vmin, vmax = volts.min(), volts.max()
offset = (vmax + vmin) / 2
vpp    = (vmax - vmin) or 1e-6            # peak-to-peak amplitude
norm   = np.clip((volts - offset) / (vpp / 2), -1, 1)

rm  = pyvisa.ResourceManager()
dev = rm.open_resource(RESOURCE)
dev.timeout = 10000
w = dev.write

w(f":OUTP{CH}:LOAD {LOAD}")
w(f":SOUR{CH}:DATA VOLATILE," + ",".join(f"{v:.5f}" for v in norm))  # upload arb
w(f":SOUR{CH}:FUNC:ARB:MODE SRATe")
w(f":SOUR{CH}:FUNC:ARB:SRAT {SRATE}")
w(f":SOUR{CH}:VOLT {vpp}")
w(f":SOUR{CH}:VOLT:OFFS {offset}")
w(f":SOUR{CH}:BURS:MODE TRIG")
w(f":SOUR{CH}:BURS:NCYC 1")
w(f":SOUR{CH}:BURS:TRIG:SOUR MAN")
w(f":SOUR{CH}:BURS:IDLE FPT")            # idle = first point = V_START  -> smooth entry
w(f":SOUR{CH}:BURS:STAT ON")
w(f":OUTP{CH} ON")                       # now resting at V_START
dev.query("*OPC?")                       # make sure setup is applied before triggering

# ---- run the arb exactly once ----
w(f":SOUR{CH}:BURS:TRIG:IMM")            # single shot: V_START -> arb -> V_END(tail)

# ---- Case B only: hand off to continuous DC during the flat tail ----
if abs(V_END - V_START) > 1e-6:
    arb_time = volts.size / SRATE
    time.sleep(arb_time - TAIL_S / 2)    # land inside the tail (already at V_END)
    w(f":SOUR{CH}:APPL:DC 1,1,{V_END}")  # continuous DC at V_END, holds indefinitely
# Case A: nothing to do — output returns to idle (== V_START == V_END) on its own.