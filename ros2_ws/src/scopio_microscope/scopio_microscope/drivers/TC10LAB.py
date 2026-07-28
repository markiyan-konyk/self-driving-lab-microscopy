"""Wavelength Electronics TC10 LAB temperature controller.

Same shape as dg1022z.DG1022Z: the object does NOT open on construction, the
node calls `_open()`. Everything public is reachable over `temperature/call`.

TWO TRANSPORTS, because on the Pi the kernel's usbtmc driver claims this
instrument and exposes it as /dev/usbtmc0. Once it has, libusb (and therefore
pyvisa-py) cannot open the device -- it HANGS, it does not error. So:

    resource = "/dev/usbtmc0"          -> kernel char device (plain SCPI on a fd)
    resource = "USB0::0x1A45::..."     -> pyvisa
    resource = "TCPIP::192.168.1.50::INSTR" -> pyvisa (Ethernet unit)
    resource = ""                      -> auto: char devices first, then USB VISA

`command()` and `query()` are the only two methods the rest of the class uses;
every other method is a one-line SCPI wrapper over them, so adding a command is
one line and clients can always fall back to raw SCPI through the same service.

Command reference: "COMMAND SET, LAB Series Instruments" (COMMAND-00400 rev H).
Units for set_temperature/temperature/limits follow set_units() -- C by default.
"""

import glob
import os
import threading

import pyvisa

READ_SIZE = 256      # keep small: the usbtmc driver reads until count or EOM

# TEC:COND? / TEC:EVEnt? bits, TC LAB column (manual p.79/81).
CONDITION_BITS = {
    0: "current_limit",
    2: "sensor_limit",
    3: "temp_high_limit",
    4: "temp_low_limit",
    5: "sensor_shorted",
    6: "sensor_open",
    7: "tec_open_circuit",
    9: "in_tolerance",
    10: "output_on",
    11: "laser_shutdown_triggered",
    15: "front_panel_power_on",
}
# Bits that mean something is WRONG (in_tolerance/output_on/power_on are normal).
FAULT_BITS = (0, 2, 3, 4, 5, 6, 7, 11)

UNITS = {0: "C", 1: "K", 2: "F", 3: "raw"}


class TC10LAB:
    def __init__(self, resource="", timeout_ms=5000):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._lock = threading.RLock()   # sessions are NOT thread-safe
        self.rm = None
        self.device = None      # pyvisa resource, when using VISA
        self.fd = None          # file descriptor, when using /dev/usbtmc*

    # ======================================================================
    # Connection (private: not reachable over the API)
    # ======================================================================
    def _open(self):
        env = os.environ.get("TCLAB_RESOURCE")
        if env:
            self.resource = env
        if not self.resource:
            self.resource = self._discover()
        if not self.resource:
            raise RuntimeError(
                "No TC10 LAB found. Checked /dev/usbtmc* and USB VISA resources. "
                "Is it powered on and plugged in? Set TCLAB_RESOURCE to name it "
                "explicitly (an Ethernet unit MUST be named: pyvisa-py cannot "
                "scan the LAN).")

        if self.resource.startswith("/dev/"):
            self.fd = os.open(self.resource, os.O_RDWR)
        else:
            self.rm = pyvisa.ResourceManager("@py")
            self.device = self.rm.open_resource(self.resource)
            self.device.read_termination = "\n"
            self.device.write_termination = "\n"
            self.device.timeout = self.timeout_ms

    def _discover(self):
        """First thing that answers *IDN? like a TC LAB. Char devices first --
        if the kernel owns the instrument, VISA will hang on it, not fail."""
        for path in sorted(glob.glob("/dev/usbtmc*")):
            fd = None
            try:
                fd = os.open(path, os.O_RDWR)
                os.write(fd, b"*IDN?\n")
                idn = os.read(fd, READ_SIZE).decode(errors="replace")
            except Exception:
                continue
            finally:
                if fd is not None:
                    os.close(fd)
            if "TC10" in idn.upper() or "WAVELENGTH" in idn.upper():
                return path
        try:
            rm = pyvisa.ResourceManager("@py")
            for res in rm.list_resources("USB?*INSTR"):
                dev = None
                try:
                    dev = rm.open_resource(res, open_timeout=self.timeout_ms)
                    dev.timeout = self.timeout_ms
                    idn = dev.query("*IDN?")
                except Exception:
                    continue
                finally:
                    if dev is not None:
                        dev.close()
                if "TC10" in idn.upper() or "WAVELENGTH" in idn.upper():
                    return res
        except Exception:
            pass
        return ""

    def _close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.device is not None:
            self.device.close()
            self.device = None
        if self.rm is not None:
            self.rm.close()
            self.rm = None

    # ======================================================================
    # Raw escape hatches -- every method below is built on these two
    # ======================================================================
    def command(self, cmd):
        """Send one SCPI command, no response. e.g. command('TEC:SET 25')"""
        with self._lock:
            if self.fd is not None:
                os.write(self.fd, cmd.encode() + b"\n")
            else:
                self.device.write(cmd)

    def query(self, cmd):
        """Send one SCPI query, return the stripped reply. e.g. query('TEC:ACT?')"""
        with self._lock:
            if self.fd is not None:
                os.write(self.fd, cmd.encode() + b"\n")
                return os.read(self.fd, READ_SIZE).decode(errors="replace").strip()
            return self.device.query(cmd).strip()

    def query_float(self, cmd):
        return float(self.query(cmd))

    def query_int(self, cmd):
        return int(float(self.query(cmd)))

    # ======================================================================
    # Identity & housekeeping
    # ======================================================================
    def idn(self):              return self.query("*IDN?")
    def model(self):            return self.query("EQUIPment?")
    def serial(self):           return self.query("SN?")
    def firmware(self):         return self.query("VER?")
    def calibration_date(self): return self.query("CALdate?")
    def uptime(self):           return self.query("TIME?")          # D:HH:MM:SS.ss
    def stopwatch(self):        return self.query("TIMER?")         # since last call
    def reset(self):            return self.command("*RST")         # factory defaults, output OFF
    def clear_status(self):     return self.command("*CLS")
    def opc(self):              return self.query("*OPC?")
    def local(self):            return self.command("LOCAL")        # give the front panel back
    def beep(self, mode=2):     return self.command(f"BEEP {int(mode)}")   # 0 off, 1 on, 2 one beep
    def brightness(self, pct):  return self.command(f"BRIGHT {int(pct)}")
    def get_brightness(self):   return self.query_int("BRIGHT?")
    def message(self, text=""): return self.command(f"MESsage {text}".rstrip())  # 32 chars on screen
    def get_message(self):      return self.query("MESsage?")
    def display(self, on):      return self.command(f"TEC:DISplay {1 if on else 0}")
    def get_display(self):      return self.query("TEC:DISplay?") == "1"
    def power_button(self, on): return self.command(f"PWR {1 if on else 0}")
    def show_remote_errors(self, on): return self.command(f"REMERR {1 if on else 0}")
    def delay(self, ms):        return self.command(f"DELAY {int(ms)}")   # 1..30000, blocks the parser

    def errors(self):
        """Drain the error queue -> '0' when clean, else e.g. '201,"Out of range"'."""
        return self.query("ERRSTR?")

    def error_codes(self):
        """Same queue, numeric codes only: '0' or '201,124'."""
        return self.query("ERRors?")

    # ======================================================================
    # The temperature loop -- the everyday methods
    # ======================================================================
    def temperature(self):      return self.query_float("TEC:ACT?")     # actual, active units
    def get_setpoint(self):     return self.query_float("TEC:SET?")
    def set_setpoint(self, degrees): return self.command(f"TEC:SET {degrees}")   # active units
    def current(self):          return self.query_float("TEC:I?")       # TEC current, A (signed)
    def voltage(self):          return self.query_float("TEC:V?")       # TEC voltage, V
    def aux_temperature(self):  return self.query_float("TEC:AUX?")     # 2nd sensor (heatsink)

    def output(self, on):
        """Enable/disable TEC current. NOTHING heats or cools until this is on.
        Overridden by the rear-panel Remote Enable input (DB-9 pin 1)."""
        return self.command(f"TEC:OUTput {1 if on else 0}")

    def output_enabled(self):   return self.query("TEC:OUTput?") == "1"

    def set_units(self, units): return self.command(f"TEC:UNITS {units}")  # 0/C 1/K 2/F 3/RAW
    def get_units(self):        return UNITS.get(self.query_int("TEC:UNITS?"), "?")

    def set_tolerance(self, deg=0.05, seconds=1.0):
        """In-tolerance window: within +/-deg for `seconds` sets condition bit 9."""
        return self.command(f"TEC:TOLerance {deg},{seconds}")

    def get_tolerance(self):    return self.query("TEC:TOLerance?")   # "deg,seconds"
    def in_tolerance(self):     return bool(self.query_int("TEC:COND?") & (1 << 9))

    def set_cable_resistance(self, ohms): return self.command(f"TEC:CABLER {ohms}")  # 0..10
    def get_cable_resistance(self):       return self.query_float("TEC:CABLER?")

    # Step / increment -- legacy but handy for slow manual ramps.
    def set_step(self, hundredths):  return self.command(f"TEC:STEP {int(hundredths)}")  # 1 == 0.01 C
    def get_step(self):              return self.query_int("TEC:STEP?")
    def step_up(self, steps=1, pause_ms=0):
        return self.command(f"TEC:INC {int(steps)},{int(pause_ms)}")
    def step_down(self, steps=1, pause_ms=0):
        return self.command(f"TEC:DEC {int(steps)},{int(pause_ms)}")

    # ======================================================================
    # Sensor selection & bias
    # ======================================================================
    def set_sensor(self, name):
        """Pick the feedback sensor by NAME. Factory names (manual p.89):
        TCS605-10/-100 (5k), TCS610-10/-100 (10k, the default), TCS620-*,
        TCS650-*, TCS651-* (100k), 'RTD 100 DIN', 'RTD 1k DIN', '*IR SENSOR',
        LM335, AD590 -- or any profile you made with add_thermistor() etc.
        The -10/-100 suffix is the bias current the calibration was taken at."""
        return self.command(f"TEC:SENSOR {name}")

    def get_sensor(self):       return self.query("TEC:SENSOR?")       # calibration coefficients
    def list_sensors(self):     return self.query("TEC:SENSORLIST?").split(",")
    def delete_sensor(self, name): return self.command(f"TEC:SENSORDEL {name}")

    def set_bias(self, code):
        """Sensor bias current: 0 auto (default), 1 10uA, 2 100uA, 3 1mA, 4 10mA.
        Any non-zero value DISABLES auto-ranging."""
        return self.command(f"TEC:BIAS {int(code)}")

    def get_bias(self):         return self.query("TEC:BIAS?")         # "AUTO,1" / "MAN,1"
    def set_aux_bias(self, code): return self.command(f"TEC:AUX:BIAS {int(code)}")
    def get_aux_bias(self):     return self.query("TEC:AUX:BIAS?")

    # ======================================================================
    # Custom sensor calibration (CONST:*) -- name is max 15 chars, no commas.
    # A sensor cannot be edited once made: delete_sensor() then re-create.
    # ======================================================================
    def add_thermistor(self, name, a, b, c):
        """Steinhart-Hart: 1/T = A + B*ln(R) + C*ln(R)^3 (from the datasheet).
        Append '.F1'/'.F2'/'.F3'/'.F4' to `name` to pin the bias current to
        10uA/100uA/1mA/10mA instead of auto-ranging.
        e.g. add_thermistor('Therm10k', 1.1279e-03, 2.3429e-04, 8.7298e-08)"""
        return self.command(f"CONST:THERM {name},{a},{b},{c}")

    def add_thermistor_points(self, name, t1, r1, t2, r2, t3, r3):
        """Same, from three (temperature C, resistance ohm) pairs -- the
        instrument fits Steinhart-Hart for you."""
        return self.command(f"CONST:THERM {name},{t1},{r1},{t2},{r2},{t3},{r3}")

    def add_rtd(self, name, standard="D", r0=100, wires=4):
        """Callendar-Van Dusen RTD. standard: 'D' DIN 43760, 'A' American,
        'I' ITS-90. r0 = resistance at 0 C. wires: 3 or 4."""
        return self.command(f"CONST:RTD{int(wires)} {name},{standard},{r0}")

    def add_rtd_linear(self, name, t1, r1, t2, r2, wires=4):
        """Linear RTD fit from two (temperature C, resistance ohm) pairs."""
        return self.command(f"CONST:RTD{int(wires)} {name},L,{t1},{r1},{t2},{r2}")

    def add_voltage_sensor(self, name, t1, v1, t2, v2):
        """LM335 / other constant-voltage sensor from two (C, V) pairs."""
        return self.command(f"CONST:ICV {name},{t1},{v1},{t2},{v2}")

    def add_optical_sensor(self, name, slope, offset):
        """Infrared optical sensor: slope V/K, offset."""
        return self.command(f"CONST:OPT {name},{slope},{offset}")

    def delete_custom_sensor(self, name): return self.command(f"CONST:DEL {name}")
    def list_custom_sensors(self):        return self.query("CONST:LIST?").split(",")

    # ======================================================================
    # PID & IntelliTune
    # ======================================================================
    def set_pid(self, p, i=None, d=None):
        """P 0.1..1000 (default 12), I 0..200 (0.1), D OFF or 1..100 (0).
        Pass p only, p+i, or all three -- the instrument reads them in order."""
        parts = [str(p)] + ([str(i)] if i is not None else []) + \
                ([str(d)] if d is not None else [])
        return self.command("TEC:PID " + ",".join(parts))

    def get_pid(self):          return self.query("TEC:PID?")          # "p,i,d"

    def set_autotune(self, mode):
        """IntelliTune method: 0 manual, 1 disturbance rejection, 2 setpoint response."""
        return self.command(f"TEC:AUTOTUNE {int(mode)}")

    def get_autotune(self):     return self.query_int("TEC:AUTOTUNE?")

    def tune_start(self):
        """Run IntelliTune in the configured mode. Preconditions (manual p.92):
        output OFF, temperature units (not RAW), setpoint at least 5 C off
        ambient. Current limits drop to 10% for the duration. Takes minutes."""
        return self.command("TEC:TUNESTART")

    def tune_abort(self):       return self.command("TEC:TUNEABORT")   # reverts to old PID
    def tune_valid(self):       return self.query("TEC:VALID?") == "1"

    # ======================================================================
    # Safety limits -- set these BEFORE enabling output
    # ======================================================================
    def set_current_limits(self, positive, negative):
        """Amps, both given POSITIVE. One of them 0 == resistive-heater mode."""
        self.command(f"TEC:LIMit:IPOS {positive}")
        return self.command(f"TEC:LIMit:INEG {negative}")

    def get_current_limits(self):
        return (self.query_float("TEC:LIMit:IPOS?"), self.query_float("TEC:LIMit:INEG?"))

    def set_temperature_limits(self, low, high):
        """Active units, -99..250 C. Exceeding one can trip the LD Shutdown BNC."""
        self.command(f"TEC:LIMit:TLO {low}")
        return self.command(f"TEC:LIMit:THI {high}")

    def get_temperature_limits(self):
        return (self.query_float("TEC:LIMit:TLO?"), self.query_float("TEC:LIMit:THI?"))

    def set_sensor_limits(self, low, high):
        """In the sensor's PHYSICAL units (ohms for a thermistor/RTD, volts for
        LM335/AD590). Note a thermistor's resistance falls as temperature rises,
        so `low` resistance == high temperature."""
        self.command(f"TEC:LIMit:RLO {low}")
        return self.command(f"TEC:LIMit:RHI {high}")

    def get_sensor_limits(self):
        return (self.query_float("TEC:LIMit:RLO?"), self.query_float("TEC:LIMit:RHI?"))

    def set_voltage_limit(self, volts):
        """Internal supply compliance. TC10 LAB rev A-C: 9..18 V, rev D: 10..27 V.
        IntelliTune sets this itself -- keep its value for best settling."""
        return self.command(f"TEC:VLIM {volts}")

    def get_voltage_limit(self): return self.query_float("TEC:VLIM?")

    def set_remote_enable_polarity(self, level):
        """Rear DB-9 pin 1 gating. 1 (default) = +5 V enables, 0 = 0 V enables."""
        return self.command(f"TEC:INTPOL {int(level)}")

    def get_remote_enable(self): return self.query("TEC:INTSTAT?")

    def set_shutdown_polarity(self, polarity):
        """LD Shutdown BNC TTL level: 0 = 5 V on fault, 1 = 0 V on fault."""
        return self.command(f"TEC:LDSHUTdown:POL {int(polarity)}")

    def get_shutdown_polarity(self): return self.query_int("TEC:LDSHUTdown:POL?")

    # ======================================================================
    # Status registers
    # ======================================================================
    def condition(self):        return self.query_int("TEC:COND?")     # live state, see CONDITION_BITS
    def event(self):            return self.query_int("TEC:EVEnt?")    # latched changes; READING CLEARS IT
    def status_byte(self):      return self.query_int("*STB?")
    def enable_condition(self, mask): return self.command(f"TEC:ENABle:COND {int(mask)}")
    def enable_event(self, mask):     return self.command(f"TEC:ENABle:EVEnt {int(mask)}")

    def faults(self):
        """Decoded TEC:COND? -> ['sensor_open', 'temp_high_limit', ...]. Empty == healthy.
        Open/short circuits are TRANSIENT: the output trips off and the bit clears,
        so a fault seen on the front panel may already be gone here -- event()
        latches those."""
        cond = self.query_int("TEC:COND?")
        return [CONDITION_BITS[b] for b in FAULT_BITS if cond & (1 << b)]

    def status(self):
        """Everything the status topic needs, in one call (7 round trips)."""
        cond = self.query_int("TEC:COND?")
        return {
            "temperature": self.query_float("TEC:ACT?"),
            "setpoint": self.query_float("TEC:SET?"),
            "current": self.query_float("TEC:I?"),
            "voltage": self.query_float("TEC:V?"),
            "units": UNITS.get(self.query_int("TEC:UNITS?"), "?"),
            "output": bool(cond & (1 << 10)),
            "in_tolerance": bool(cond & (1 << 9)),
            "condition": cond,
            "faults": [CONDITION_BITS[b] for b in FAULT_BITS if cond & (1 << b)],
        }

    # ======================================================================
    # Stored profiles (1..10; 0 is the read-only factory profile)
    # ======================================================================
    def save_profile(self, n):    return self.command(f"*SAV {int(n)}")
    def recall_profile(self, n):  return self.command(f"*RCL {int(n)}")   # output shuts off
    def name_profile(self, n, line1="", line2=""):
        return self.command(f"PROFile:DESC {int(n)},{line1},{line2}")
    def get_profile_name(self, n):     return self.query(f"PROFile:DESC? {int(n)}")
    def profile_setpoint(self, n, c):  return self.command(f"PROFile:SET {int(n)},{c}")
    def get_profile_setpoint(self, n): return self.query_float(f"PROFile:SET? {int(n)}")
    def profile_pid(self, n, p, i, d): return self.command(f"PROFile:PID {int(n)},{p},{i},{d}")
    def get_profile_pid(self, n):      return self.query(f"PROFile:PID? {int(n)}")
    def profile_sensor(self, n, name): return self.command(f"PROFile:SENsor {int(n)},{name}")
    def profile_units(self, n, u):     return self.command(f"PROFile:UNITS {int(n)},{u}")
    def profile_tolerance(self, n, deg, seconds):
        return self.command(f"PROFile:TOLerance {int(n)},{deg},{seconds}")
    def profile_current_limits(self, n, positive, negative):
        self.command(f"PROFile:IPOS {int(n)},{positive}")
        return self.command(f"PROFile:INEG {int(n)},{negative}")
    def profile_temperature_limits(self, n, low, high):
        self.command(f"PROFile:TLO {int(n)},{low}")
        return self.command(f"PROFile:THI {int(n)},{high}")

    # ======================================================================
    # Instrument-side scans and scripts
    # ======================================================================
    def profile_scan(self, n, start, stop, step, wait_s):
        """Configure the front-panel temperature scan stored in profile n.
        wait_s = 0 means 'wait until in tolerance' instead of a fixed dwell.
        Scans cannot run in RAW units."""
        self.command(f"PROFile:SCANSTART {int(n)},{start}")
        self.command(f"PROFile:SCANSTOP {int(n)},{stop}")
        self.command(f"PROFile:SCANSTEP {int(n)},{step}")
        return self.command(f"PROFile:SCANWAIT {int(n)},{wait_s}")

    def put_script(self, index, script):
        """Store a script (index 1..4, max 200 chars). Commands are separated by
        CARATS, not semicolons, and the command path is not repeated:
            put_script(1, 'TEC:SET 25^LIMit:IPOS 3.2^OUT 1')"""
        return self.command(f"SCRIPT:PUT {int(index)},{script}")

    def run_script(self, index):  return self.command(f"SCRIPT:GO {int(index)}")
    def get_script(self, index):  return self.query(f"SCRIPT:GET? {int(index)}")

    # ======================================================================
    # Network settings (take effect after a rear-panel power cycle)
    # ======================================================================
    def get_ip(self):           return self.query("TECH:IPADDR?")
    def set_ip(self, addr):     return self.command(f"TECH:IPADDR {addr}")
    def get_netmask(self):      return self.query("TECH:IPMASK?")
    def set_netmask(self, m):   return self.command(f"TECH:IPMASK {m}")
    def get_gateway(self):      return self.query("TECH:IPGW?")
    def set_gateway(self, g):   return self.command(f"TECH:IPGW {g}")
    def get_mac(self):          return self.query("TECH:HWADDR?")


# ================================================================================
# Bench smoke test:  python3 TC10LAB.py  [resource]
# ================================================================================
if __name__ == "__main__":
    import sys

    tc = TC10LAB(sys.argv[1] if len(sys.argv) > 1 else "")
    tc._open()
    print("resource :", tc.resource)
    print("idn      :", tc.idn())
    print("sensor   :", tc.get_sensor())
    print("units    :", tc.get_units())
    print("status   :", tc.status())
    print("errors   :", tc.errors())
    tc.local()
    tc._close()
