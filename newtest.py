import pyvisa, numpy as np, time

RESOURCE = "USB0::0x1AB1::0x0642::DG1ZXXXXXXXXX::INSTR"  # your real address
CH       = 1
LOAD     = "INF"
V_START  = 0.0
V_END    = 1.0
SRATE    = 1e5
TAIL_S   = 0.05

t = np.linspace(0, 1, 2000)
body = V_START + (V_END - V_START) * (1 - np.cos(np.pi * t)) / 2
tail = np.full(int(TAIL_S * SRATE), V_END)
volts = np.concatenate([body, tail])
assert volts.size <= 16384, "DG1022Z volatile arb max is 16k points"

vmin, vmax = volts.min(), volts.max()
offset = (vmax + vmin) / 2
vpp    = (vmax - vmin) or 1e-6
norm   = np.clip((volts - offset) / (vpp / 2), -1, 1)
dac    = np.clip(np.round((norm + 1) / 2 * 16383), 0, 16383).astype(np.uint16)  # 0..16383

rm  = pyvisa.ResourceManager('@py')
dev = rm.open_resource(RESOURCE)
dev.timeout = 20000
print("Connected:", dev.query("*IDN?").strip())
w = dev.write

w(f":OUTP{CH}:LOAD {LOAD}")
# --- binary DAC block upload (little-endian 16-bit, values 0..16383) ---
dev.write_binary_values(f":SOUR{CH}:DATA:DAC VOLATILE,", dac,
                        datatype='H', is_big_endian=False)
dev.query("*OPC?")
print("Upload error status:", dev.query(":SYST:ERR?").strip())   # want "0,No error"

w(f":SOUR{CH}:FUNC:ARB:MODE SRATe")
w(f":SOUR{CH}:FUNC:ARB:SRAT {SRATE}")
w(f":SOUR{CH}:VOLT {vpp}")
w(f":SOUR{CH}:VOLT:OFFS {offset}")
w(f":SOUR{CH}:BURS:MODE TRIG")
w(f":SOUR{CH}:BURS:NCYC 1")
w(f":SOUR{CH}:BURS:TRIG:SOUR MAN")
w(f":SOUR{CH}:BURS:IDLE FPT")
w(f":SOUR{CH}:BURS:STAT ON")
w(f":OUTP{CH} ON")
dev.query("*OPC?")

w(f":SOUR{CH}:BURS:TRIG:IMM")

if abs(V_END - V_START) > 1e-6:
    arb_time = volts.size / SRATE
    time.sleep(arb_time - TAIL_S / 2)
    w(f":SOUR{CH}:APPL:DC 1,1,{V_END}")