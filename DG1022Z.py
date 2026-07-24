"""
wavegen.py - Clean SCPI driver for the Rigol DG1022Z function/arbitrary waveform generator.

The DG1022Z is the 25 MHz, 2-channel member of Rigol's DG1000Z series; every command
below is mapped from the DG1000Z Programming Guide (RIGOL, pub. PGB09106). Numeric
queries return decimal/scientific notation - there is no hex/2's-complement return mode
on this series to disable.

Install:
    pip install pyvisa pyvisa-py         # pure-Python backend, no NI-VISA needed

Quick start:
    from wavegen import WaveGen
    gen = WaveGen("TCPIP::192.168.1.10::INSTR")          # Ethernet: preferred in Docker
    gen.reset()
    gen.apply_sine(freq=1_000, amp=2.0, offset=0.0, phase=0.0, channel=1)
    gen.set_output_load(1, gen.HIGH_Z); gen.output(True, 1)   # amplitude ~doubles vs 50 ohm!
"""

import time
import threading
import pyvisa
import os
import readchar

class DG1022Z:
    CH1, CH2 = 1, 2

    SINE = "SIN"; SQUARE = "SQU"; RAMP = "RAMP"; PULSE = "PULS"
    NOISE = "NOIS"; DC = "DC"; USER = "USER"; HARMONIC = "HARM"

    HIGH_Z = "INFinity"          # High-Z; pass a number (e.g. 50) for a fixed load
    LOAD_50 = 50

    TRIG_INTERNAL = "INTernal"; TRIG_EXTERNAL = "EXTernal"; TRIG_MANUAL = "MANual"
    MOD_INTERNAL = "INTernal"; MOD_EXTERNAL = "EXTernal"

    SWEEP_LINEAR = "LINear"; SWEEP_LOG = "LOGarithmic"; SWEEP_STEP = "STEp"
    BURST_TRIGGERED = "TRIGgered"; BURST_GATED = "GATed"; BURST_INFINITE = "INFinity"

    VPP = "VPP"; VRMS = "VRMS"; DBM = "DBM"
    NORMAL = "NORMal"; INVERTED = "INVerted"

    def __init__(self, resource="", timeout_ms=5000, backoff_s=0.5):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._backoff_s = backoff_s
        self._lock = threading.RLock()   # VISA sessions are NOT thread-safe
        self.reconnects = 0
        self.rm = None
        self.device = None

        self.xoffset:float = 0
        self.yoffset:float = 0


    def _open(self):
        self.rm = pyvisa.ResourceManager("@py")
        env = os.environ.get("DAC_ID")
        if env:
            self.resource = env
        if not self.resource:
            resources = self.rm.list_resources('USB?*INSTR')
            if not resources:
                raise RuntimeError("No USB VISA instruments found. Check physical connection.")
            self.resource = resources[0]
            self.device = self.rm.open_resource(self.resource)
        else:
            self.device = self.rm.open_resource(self.resource)
        self.device.read_termination = "\n"
        self.device.write_termination = "\n"
        self.device.timeout = self.timeout_ms

    def _open_debug(self):
        self.rm = pyvisa.ResourceManager("@py")
        env = os.environ.get("DAC_ID")
        if env:
            self.resource = env
        else:
            print("No os.environ input detected")
        
        if not self.resource:
            resources = self.rm.list_resources()
            print("ALL VISA resources:", resources or "(none found)")
            resources = self.rm.list_resources('USB?*INSTR')
            print("VISA resources starting with USB:", resources or "(none found)")
            if not resources:
                raise RuntimeError("No USB VISA instruments found. Check physical connection.")
            print(type(resources[0]))
            print(f"Using the device with ID:{resources[0]}")
            self.device= self.rm.open_resource(resources[0])
            print(f"Using the device with ID:{resources[0]}")
        else:
            print(f"Using the device with ID:{resources[0]}")
            self.device = self.rm.open_resource(self.resource)
            print(f"Using the device with ID:{self.resource}")

        self.device.read_termination = "\n"
        self.device.write_termination = "\n"
        self.device.timeout = self.timeout_ms
        print(f"Timeout set to {self.timeout_ms}")
        id = self.device.query("*IDN?").strip()
        print(f"IDN:{id}")
        if "DG1" not in id.upper():
            print("WARNING: this does not look like a DG1022Z. Double-check the "
                  "resource address.")
        print("OK - connection works.")

    def _close(self):
        self.device.write(":OUTP1 OFF;:OUTP2 OFF")
        self.device.close()

    def _calibrate_offset(self):
        self.dcinit()
        print("Calibrate the X axis")
        print(f"Starting at {self.xoffset}")
        print("Controls: [UP/DOWN] Change number | [s] Save\n")
        while True:
            key = readchar.readkey()

            if key == readchar.key.UP:
                self.xoffset += 0.01
                print(f"Current offset: {self.xoffset}    ", end='\r') 
                
            elif key == readchar.key.DOWN:
                self.xoffset -= 0.01
                print(f"Current offset: {self.xoffset}    ", end='\r')
                
            elif key.lower() == 's':
                print(f"\n[Saved] Number stored as: {self.xoffset}")
                print(f"Current number: {self.xoffset}    ", end='\r')
                break

        print("Calibrate the X axis")
        print(f"Starting at {self.yoffset}")
        while True:
            key = readchar.readkey()

            if key == readchar.key.UP:
                self.yoffset += 0.01
                print(f"Current offset: {self.yoffset}    ", end='\r') 
                
            elif key == readchar.key.DOWN:
                self.yoffset -= 0.01
                print(f"Current offset: {self.yoffset}    ", end='\r')
                
            elif key.lower() == 's':
                print(f"\n[Saved] Number stored as: {self.yoffset}")
                print(f"Current number: {self.yoffset}    ", end='\r')
                break   

    def dcinit(self):
        self.device.write(f":OUTPut1:LOAD INFinity;:OUTPut2:LOAD INFinity")
        self.device.write(f":SOURce1:APPLy:DC 1,1,{self.xoffset:.3f}")
        self.device.write(f":SOURce2:APPLy:DC 1,1,{self.yoffset:.3f}")
        self.device.write(":OUTP1 ON;:OUTP2 ON")

    def dcupdate(self, ch:int, val:float):
        self.device.write(f"SOURce{ch}:VOLTage:OFFSet {val:.3f}")
'''
    def _recover(self):
        # A single hiccup must not crash the app: abort/clear the stalled USBTMC
        # session, then fully reconnect with a short backoff before the caller retries.
        self.reconnects += 1
        try:
            self.device.clear()          # USBTMC abort/clear - un-wedges a stalled session
        except Exception:
            pass
        try:
            self.device.close()
        except Exception:
            pass
        time.sleep(self._backoff_s)
        self._open()

    def _io(self, action):
        # Every read/write goes through here: locked, and auto-recovered once on I/O error.
        with self._lock:
            try:
                return action()
            except (pyvisa.errors.VisaIOError, OSError):
                self._recover()
                return action()        # retry the call exactly once

    def close(self):
        try:
            if self.device is not None:
                self.device.close()
        finally:
            if self.rm is not None:
                self.rm.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ============================================================================
    # Raw escape hatches (every other method is built on these two)
    # ============================================================================
    def command(self, cmd):
        """Send one SCPI command, no response."""
        return self._io(lambda: self.device.write(cmd))

    def query(self, cmd):
        """Send one SCPI query, return the stripped string response."""
        return self._io(lambda: self.device.query(cmd).strip())

    def query_float(self, cmd):
        return float(self.query(cmd))

    @staticmethod
    def _b(state):
        return "ON" if state else "OFF"

    @staticmethod
    def _load(v):
        return v if isinstance(v, str) else f"{v}"

    @staticmethod
    def _src(ch):
        return f":SOUR{int(ch)}"

    # ============================================================================
    # Identity & housekeeping
    # ============================================================================
    def idn(self):                 return self.query("*IDN?")
    def reset(self):               return self.command("*RST")
    def clear_status(self):        return self.command("*CLS")
    def wait(self):                return self.command("*WAI")
    def opc(self):                 return self.query("*OPC?")          # returns "1" when done
    def save_state(self, loc):     return self.command(f"*SAV {int(loc)}")   # loc 0..5, non-volatile
    def recall_state(self, loc):   return self.command(f"*RCL {int(loc)}")   # loc 0..5

    def get_errors(self):
        """Drain the SCPI error queue; return a list of (code, message). Empty == clean."""
        errors = []
        for _ in range(64):                          # bounded so a stuck queue can't hang us
            resp = self.query(":SYSTem:ERRor?")      # standard SCPI on the DG1000Z
            code, _, msg = resp.partition(",")
            if int(code) == 0:                       # 0,"No error"
                break
            errors.append((int(code), msg.strip().strip('"')))
        return errors

    # ============================================================================
    # Channel setup  (channel defaults to 1; DG1022Z has 2 channels)
    # ============================================================================
    # One-shot "apply" configures shape + key params in a single command:
    def apply_sine(self, freq, amp, offset=0.0, phase=0.0, channel=1):
        return self.command(f"{self._src(channel)}:APPL:SIN {freq},{amp},{offset},{phase}")

    def apply_square(self, freq, amp, offset=0.0, phase=0.0, channel=1):
        return self.command(f"{self._src(channel)}:APPL:SQU {freq},{amp},{offset},{phase}")

    def apply_ramp(self, freq, amp, offset=0.0, phase=0.0, channel=1):
        return self.command(f"{self._src(channel)}:APPL:RAMP {freq},{amp},{offset},{phase}")

    def apply_pulse(self, freq, amp, offset=0.0, phase=0.0, channel=1):
        return self.command(f"{self._src(channel)}:APPL:PULS {freq},{amp},{offset},{phase}")

    def apply_noise(self, amp, offset=0.0, channel=1):     # noise takes amp+offset only
        return self.command(f"{self._src(channel)}:APPL:NOIS {amp},{offset}")

    def apply_dc(self, offset, channel=1):                 # freq/amp are ignored placeholders
        return self.command(f"{self._src(channel)}:APPL:DC 1,1,{offset}")

    # Granular setters (each wraps one command; read like a menu):
    def set_function(self, shape, channel=1):    return self.command(f"{self._src(channel)}:FUNC {shape}")
    def get_function(self, channel=1):           return self.query(f"{self._src(channel)}:FUNC?")
    def set_frequency(self, freq, channel=1):    return self.command(f"{self._src(channel)}:FREQ {freq}")
    def get_frequency(self, channel=1):          return self.query_float(f"{self._src(channel)}:FREQ?")
    def set_period(self, seconds, channel=1):    return self.command(f"{self._src(channel)}:PER {seconds}")
    def set_amplitude(self, amp, channel=1):     return self.command(f"{self._src(channel)}:VOLT {amp}")
    def get_amplitude(self, channel=1):          return self.query_float(f"{self._src(channel)}:VOLT?")
    def set_offset(self, offset, channel=1):     return self.command(f"{self._src(channel)}:VOLT:OFFS {offset}")
    def get_offset(self, channel=1):             return self.query_float(f"{self._src(channel)}:VOLT:OFFS?")
    def set_phase(self, deg, channel=1):         return self.command(f"{self._src(channel)}:PHAS {deg}")
    def get_phase(self, channel=1):              return self.query_float(f"{self._src(channel)}:PHAS?")
    def set_voltage_unit(self, unit, channel=1): return self.command(f"{self._src(channel)}:VOLT:UNIT {unit}")  # VPP/VRMS/DBM
    def set_high_level(self, v, channel=1):      return self.command(f"{self._src(channel)}:VOLT:HIGH {v}")
    def set_low_level(self, v, channel=1):       return self.command(f"{self._src(channel)}:VOLT:LOW {v}")

    # Shape parameters:
    def set_duty_cycle(self, pct, channel=1):        return self.command(f"{self._src(channel)}:FUNC:SQU:DCYC {pct}")   # square
    def set_ramp_symmetry(self, pct, channel=1):     return self.command(f"{self._src(channel)}:FUNC:RAMP:SYMM {pct}")  # ramp
    def set_pulse_width(self, seconds, channel=1):   return self.command(f"{self._src(channel)}:PULS:WIDT {seconds}")
    def set_pulse_duty(self, pct, channel=1):        return self.command(f"{self._src(channel)}:PULS:DCYC {pct}")
    def set_pulse_leading_edge(self, s, channel=1):  return self.command(f"{self._src(channel)}:PULS:TRAN:LEAD {s}")
    def set_pulse_trailing_edge(self, s, channel=1): return self.command(f"{self._src(channel)}:PULS:TRAN:TRA {s}")

    # Output control:
    def output(self, on, channel=1):             return self.command(f":OUTP{int(channel)} {self._b(on)}")
    def get_output(self, channel=1):             return self.query(f":OUTP{int(channel)}?") in ("ON", "1")
    def set_output_polarity(self, pol, channel=1): return self.command(f":OUTP{int(channel)}:POL {pol}")
    def set_sync(self, on, channel=1):           return self.command(f":OUTP{int(channel)}:SYNC {self._b(on)}")

    def set_output_load(self, channel, ohms_or_INF):
        """Set output load. FOOTGUN: amplitude is defined into this load - a level set for
        50 ohm roughly DOUBLES into High-Z. Pass a number (e.g. 50) or gen.HIGH_Z. Range 1 - 10k."""
        return self.command(f":OUTP{int(channel)}:LOAD {self._load(ohms_or_INF)}")

    # ============================================================================
    # Modulation  (AM / FM / PM / PWM / FSK - enable + configure per subsystem)
    # ============================================================================
    def set_am(self, on, channel=1):        return self.command(f"{self._src(channel)}:AM:STAT {self._b(on)}")
    def am_depth(self, pct, channel=1):     return self.command(f"{self._src(channel)}:AM {pct}")           # depth %
    def am_source(self, src, channel=1):    return self.command(f"{self._src(channel)}:AM:SOUR {src}")
    def am_mod_freq(self, hz, channel=1):   return self.command(f"{self._src(channel)}:AM:INT:FREQ {hz}")
    def am_mod_shape(self, sh, channel=1):  return self.command(f"{self._src(channel)}:AM:INT:FUNC {sh}")

    def set_fm(self, on, channel=1):        return self.command(f"{self._src(channel)}:FM:STAT {self._b(on)}")
    def fm_deviation(self, hz, channel=1):  return self.command(f"{self._src(channel)}:FM {hz}")            # deviation Hz
    def fm_source(self, src, channel=1):    return self.command(f"{self._src(channel)}:FM:SOUR {src}")
    def fm_mod_freq(self, hz, channel=1):   return self.command(f"{self._src(channel)}:FM:INT:FREQ {hz}")
    def fm_mod_shape(self, sh, channel=1):  return self.command(f"{self._src(channel)}:FM:INT:FUNC {sh}")

    def set_pm(self, on, channel=1):        return self.command(f"{self._src(channel)}:PM:STAT {self._b(on)}")
    def pm_deviation(self, deg, channel=1): return self.command(f"{self._src(channel)}:PM {deg}")           # deviation deg
    def pm_source(self, src, channel=1):    return self.command(f"{self._src(channel)}:PM:SOUR {src}")
    def pm_mod_freq(self, hz, channel=1):   return self.command(f"{self._src(channel)}:PM:INT:FREQ {hz}")
    def pm_mod_shape(self, sh, channel=1):  return self.command(f"{self._src(channel)}:PM:INT:FUNC {sh}")

    def set_pwm(self, on, channel=1):       return self.command(f"{self._src(channel)}:PWM:STAT {self._b(on)}")
    def pwm_deviation(self, pct, channel=1):return self.command(f"{self._src(channel)}:PWM:DEV:DCYC {pct}") # duty deviation %
    def pwm_source(self, src, channel=1):   return self.command(f"{self._src(channel)}:PWM:SOUR {src}")
    def pwm_mod_freq(self, hz, channel=1):  return self.command(f"{self._src(channel)}:PWM:INT:FREQ {hz}")

    def set_fsk(self, on, channel=1):       return self.command(f"{self._src(channel)}:FSK:STAT {self._b(on)}")
    def fsk_hop_freq(self, hz, channel=1):  return self.command(f"{self._src(channel)}:FSK:FREQ {hz}")      # hop frequency
    def fsk_rate(self, hz, channel=1):      return self.command(f"{self._src(channel)}:FSK:RATE {hz}")
    def fsk_source(self, src, channel=1):   return self.command(f"{self._src(channel)}:FSK:SOUR {src}")

    # ============================================================================
    # Sweep
    # ============================================================================
    def set_sweep(self, on, channel=1):            return self.command(f"{self._src(channel)}:SWE:STAT {self._b(on)}")
    def sweep_start_freq(self, hz, channel=1):     return self.command(f"{self._src(channel)}:FREQ:STAR {hz}")
    def sweep_stop_freq(self, hz, channel=1):      return self.command(f"{self._src(channel)}:FREQ:STOP {hz}")
    def sweep_time(self, seconds, channel=1):      return self.command(f"{self._src(channel)}:SWE:TIME {seconds}")
    def sweep_spacing(self, mode, channel=1):      return self.command(f"{self._src(channel)}:SWE:SPAC {mode}")   # LIN/LOG/STE
    def sweep_trigger_source(self, src, channel=1):return self.command(f"{self._src(channel)}:SWE:TRIG:SOUR {src}")

    # ============================================================================
    # Burst
    # ============================================================================
    def set_burst(self, on, channel=1):            return self.command(f"{self._src(channel)}:BURS:STAT {self._b(on)}")
    def burst_mode(self, mode, channel=1):         return self.command(f"{self._src(channel)}:BURS:MODE {mode}")  # TRIG/GAT/INF
    def burst_cycles(self, n, channel=1):          return self.command(f"{self._src(channel)}:BURS:NCYC {int(n)}")
    def burst_phase(self, deg, channel=1):         return self.command(f"{self._src(channel)}:BURS:PHAS {deg}")
    def burst_trigger_source(self, src, channel=1):return self.command(f"{self._src(channel)}:BURS:TRIG:SOUR {src}")
    def burst_period(self, seconds, channel=1):    return self.command(f"{self._src(channel)}:BURS:INT:PER {seconds}")
    def burst_trigger_out(self, edge, channel=1):  return self.command(f"{self._src(channel)}:BURS:TRIG:TRIGO {edge}")  # OFF/POS/NEG

    # ============================================================================
    # Arbitrary waveforms
    # ============================================================================
    def upload_arb(self, points, channel=1, normalized=True):
        """Upload an arb to volatile memory as a binary DAC16 block, then select it.
        `points`: floats in [-1, 1] (normalized=True) mapped to the 14-bit DAC, or raw
        ints 0..16383 (normalized=False). Binary is fast/exact for long waveforms."""
        if normalized:
            dac = [max(0, min(16383, int(round((p + 1.0) * 0.5 * 16383)))) for p in points]
        else:
            dac = [max(0, min(16383, int(p))) for p in points]
        # DAC16 words are 16-bit little-endian, valid range 0..16383; END = final block.
        prefix = f"{self._src(channel)}:TRAC:DATA:DAC16 VOLATILE,END,"
        self._io(lambda: self.device.write_binary_values(prefix, dac, datatype="H", is_big_endian=False))
        self.query("*OPC?")                                    # wait for transfer to finish
        self.command(f"{self._src(channel)}:FUNC USER")        # ASSUMPTION: selects the volatile arb

    def upload_arb_ascii(self, points, channel=1):
        """Same as upload_arb but sends normalized floats [-1, 1] as an ASCII block (simpler, slower)."""
        vals = ",".join(f"{p:.6f}" for p in points)
        self.command(f"{self._src(channel)}:TRAC:DATA VOLATILE,{vals}")
        self.query("*OPC?")
        self.command(f"{self._src(channel)}:FUNC USER")        # ASSUMPTION: selects the volatile arb

    def select_arb(self, name, channel=1):
        """Select a built-in / stored arb by its documented name (e.g. 'EXPRISE', 'CARDIAC')."""
        return self.command(f"{self._src(channel)}:FUNC {name}")

    # ============================================================================
    # Trigger / sync / inter-channel
    # ============================================================================
    def bus_trigger(self):                   return self.command("*TRG")                       # fires armed sweep/burst
    def manual_trigger(self, channel=1):     return self.command(f"{self._src(channel)}:BURS:TRIG")  # burst software trigger
    def trigger_sweep(self, channel=1):      return self.command(f"{self._src(channel)}:SWE:TRIG")
    def align_phase(self, channel=1):        return self.command(f"{self._src(channel)}:PHAS:SYNC")  # zero inter-channel phase
    def set_track(self, mode, channel=1):    return self.command(f"{self._src(channel)}:TRACK {mode}")  # ON/OFF/INVerted
    def set_coupling(self, on):              return self.command(f":COUP:STAT {self._b(on)}")   # freq/amp/phase coupling

    # ============================================================================
    # Burst-safe command pacing
    # ============================================================================
    def send_sequence(self, commands, pace_s=0.002, check_every=64):
        """Pace a long, rapid command stream (e.g. stepping a DC level thousands of times to
        move a galvo). A tight write() loop is exactly what wedges naive USBTMC apps; this
        spaces the writes, periodically drains the error queue, and issues *OPC? after the
        burst. Prefer this over a raw loop. Returns any errors collected.

        Example:  gen.send_sequence([f":SOUR1:VOLT:OFFS {v}" for v in levels])
        """
        errors = []
        with self._lock:                                 # hold the session for the whole burst
            for i, cmd in enumerate(commands, 1):
                self.command(cmd)
                if pace_s:
                    time.sleep(pace_s)
                if check_every and (i % check_every == 0):
                    errors.extend(self.get_errors())
            self.query("*OPC?")                          # confirm the instrument finished
            errors.extend(self.get_errors())
        return errors


# ================================================================================
# Smoke test
# ================================================================================
if __name__ == "__main__":
    with DG1022Z() as gen:                 # edit the default resource string to match your unit
        print("IDN          :", gen.idn())
        print("CH1 function :", gen.get_function(1))
        print("CH1 frequency:", gen.get_frequency(1))
        print("CH1 load     :", gen.query(":OUTP1:LOAD?"))
        print("Errors       :", gen.get_errors())
        print("Reconnects   :", gen.reconnects)
        '''