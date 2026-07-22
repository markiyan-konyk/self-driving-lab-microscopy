"""
tc_lab.py - One clean class to drive the Wavelength Electronics TC LAB
temperature controller over VISA (USBTMC on USB, or VXI-11 on Ethernet).

There is one plainly-named method for each thing you'd actually do. For any
command not wrapped here, use .command("...") or .query("...").

The only "driver" cleverness kept, because it earns its place:
  * a lock, so concurrent calls can't interleave and corrupt the protocol
  * auto-recovery (device clear, then reconnect) if the link hiccups
  * an error-queue check that reads ERRSTR? (this box does NOT use SYST:ERR?)

Install:  pip install pyvisa pyvisa-py pyusb      (+ libusb on Linux for USB)

Quick start:
    from tc_lab import TCLab
    with TCLab("TCPIP::192.168.1.50::INSTR") as tc:   # or "USB0::0x1A45::...::INSTR"
        tc.set_units(tc.CELSIUS)
        tc.set_setpoint(25.0)
        tc.output(True)
        print(tc.temperature())
"""

import time
import threading
import pyvisa

_IO_ERRORS = (pyvisa.errors.VisaIOError, OSError, ConnectionError, EOFError)


class TCLabError(RuntimeError):
    pass


class TCLab:
    # --- handy constants ---
    MANUAL, DISTURBANCE_REJECTION, SETPOINT_RESPONSE = 0, 1, 2   # IntelliTune modes
    CELSIUS, KELVIN, FAHRENHEIT, RAW = 0, 1, 2, 3                # units
    BIAS_AUTO, BIAS_10UA, BIAS_100UA, BIAS_1MA, BIAS_10MA = 0, 1, 2, 3, 4

    # ================================================================== #
    #  Connection (with lock + auto-recovery)
    # ================================================================== #
    def __init__(self, resource, backend="@py", timeout_ms=5000):
        self._resource = resource
        self._backend = backend
        self._timeout_ms = timeout_ms
        self._rm = None
        self._dev = None
        self._lock = threading.RLock()
        self.reconnects = 0
        self._open()
        self.idn = self.query("*IDN?")

    def _open(self):
        self._close_quiet()
        self._rm = pyvisa.ResourceManager(self._backend)
        self._dev = self._rm.open_resource(self._resource)
        self._dev.timeout = self._timeout_ms
        self._dev.read_termination = "\n"
        self._dev.write_termination = "\n"

    def _close_quiet(self):
        for obj in (self._dev, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._dev = self._rm = None

    def _recover(self):
        # cheap fix first: a device clear un-wedges a stalled USBTMC session
        try:
            self._dev.clear()
            return
        except Exception:
            pass
        # otherwise reconnect with a short backoff
        for delay in (0.5, 1.0, 2.0):
            time.sleep(delay)
            try:
                self._open()
                self.reconnects += 1
                return
            except _IO_ERRORS:
                continue
        raise TCLabError(f"Could not reconnect to {self._resource}")

    # ---- the two primitives everything else is built on ----
    def command(self, cmd):
        """Send any command (no reply). Escape hatch for unwrapped commands."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    return self._dev.write(cmd)
                except _IO_ERRORS as e:
                    if attempt:
                        raise TCLabError(f"write failed: {e}") from e
                    self._recover()

    def query(self, cmd):
        """Send any query and return the stripped reply. Escape hatch."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    return self._dev.query(cmd).strip()
                except _IO_ERRORS as e:
                    if attempt:
                        raise TCLabError(f"query failed: {e}") from e
                    self._recover()

    def close(self):
        try:
            self.local()
        except Exception:
            pass
        self._close_quiet()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ================================================================== #
    #  Identity, housekeeping, errors
    # ================================================================== #
    def identity(self):            return self.query("*IDN?")
    def serial_number(self):       return self.query("SN?")
    def firmware_version(self):    return self.query("VER?")
    def equipment(self):           return self.query("EQUIPment?")
    def calibration_date(self):    return self.query("CALdate?")
    def uptime(self):              return self.query("TIME?")
    def timer(self):               return self.query("TIMER?")

    def reset(self):               self.command("*RST")   # factory defaults, output off
    def clear_status(self):        self.command("*CLS")   # clear registers + error queue
    def local(self):               self.command("LOCAL")  # hand control back to touchscreen

    def wait_until_done(self, timeout_s=30):
        """Block until all pending operations complete (*OPC?)."""
        old = self._dev.timeout
        try:
            self._dev.timeout = int(timeout_s * 1000)
            self.query("*OPC?")
        finally:
            self._dev.timeout = old

    def ping(self):
        try:
            self.query("*OPC?")
            return True
        except TCLabError:
            return False

    def get_errors(self):
        """Return list of 'code,text' error strings; [] if clean. Drains the queue."""
        out = []
        for _ in range(32):
            resp = self.query("ERRSTR?")
            if resp.split(",")[0].strip() in ("0", ""):
                break
            out.append(resp)
        return out

    def raise_on_error(self):
        errs = self.get_errors()
        if errs:
            raise TCLabError("Instrument errors: " + " | ".join(errs))

    # ================================================================== #
    #  Front panel: power, display, sound, misc
    # ================================================================== #
    def power(self, on):           self.command(f"PWR {1 if on else 0}")  # rear switch must be on too
    def power_on(self):            return self.query("PWR?") == "1"
    def display(self, on):         self.command(f"TEC:DISplay {1 if on else 0}")
    def display_on(self):          return self.query("TEC:DISplay?") == "1"
    def set_brightness(self, pct): self.command(f"BRIGHT {pct}")          # 0-100
    def get_brightness(self):      return int(self.query("BRIGHT?"))
    def beep(self, mode=2):        self.command(f"BEEP {mode}")           # 0 off, 1 on, 2 one beep
    def beep_enabled(self):        return self.query("BEEP?") == "1"
    def set_message(self, text):   self.command(f"MESsage {text}")        # up to 32 chars on screen
    def get_message(self):         return self.query("MESsage?")
    def set_hexfloat(self, on):    self.command(f"HEXFLOAT {1 if on else 0}")  # keep OFF
    def hexfloat(self):            return self.query("HEXFLOAT?") == "1"
    def delay_ms(self, ms):        self.command(f"DELAY {ms}")            # on-instrument delay

    # ================================================================== #
    #  Network configuration (power-cycle for changes to take effect)
    # ================================================================== #
    def ip_address(self):          return self.query("TECH:IPADDR?")
    def set_ip_address(self, ip):  self.command(f"TECH:IPADDR {ip}")
    def subnet_mask(self):         return self.query("TECH:IPMASK?")
    def set_subnet_mask(self, m):  self.command(f"TECH:IPMASK {m}")
    def gateway(self):             return self.query("TECH:IPGW?")
    def set_gateway(self, gw):     self.command(f"TECH:IPGW {gw}")
    def mac_address(self):         return self.query("TECH:HWADDR?")
    def flashdrive_present(self):  return self.query("TECH:FLASHDRIVE?") == "1"

    # ================================================================== #
    #  Units & sensors
    # ================================================================== #
    def set_units(self, u):        self.command(f"TEC:UNITS {u}")        # CELSIUS/KELVIN/FAHRENHEIT/RAW
    def get_units(self):           return int(self.query("TEC:UNITS?"))
    def select_sensor(self, name): self.command(f"TEC:SENSOR {name}")    # e.g. "TCS-610-100", "RTD 100 DIN"
    def get_sensor_coeffs(self):   return self.query("TEC:SENSOR?")
    def list_sensors(self):        return self.query("TEC:SENSORLIST?").split(",")
    def list_custom_sensors(self): return self.query("CONST:LIST?").split(",")
    def delete_sensor(self, name): self.command(f"CONST:DEL {name}")

    def set_main_bias(self, rng):  self.command(f"TEC:BIAS {rng}")       # BIAS_AUTO..BIAS_10MA
    def get_main_bias(self):       return self.query("TEC:BIAS?")
    def set_aux_bias(self, rng):   self.command(f"TEC:AUX:BIAS {rng}")
    def get_aux_bias(self):        return self.query("TEC:AUX:BIAS?")

    def create_thermistor_sensor(self, name, a, b, c):
        """Steinhart-Hart thermistor profile (15-char name max)."""
        self.command(f"CONST:THERM {name},{a},{b},{c}")

    def create_rtd_sensor(self, name, wires=2, standard="D", r0=1000.0):
        """Callendar-Van Dusen RTD. wires=2/3/4; standard D(DIN)/I(ITS-90)/A(American)."""
        self.command(f"CONST:RTD{wires} {name},{standard},{r0}")

    # (for LM335 / AD590 / optical customs, use .command('CONST:ICV ...') etc.)

    # ================================================================== #
    #  Setpoint & output control
    # ================================================================== #
    def set_setpoint(self, value): self.command(f"TEC:SET {value}")      # in active units
    def get_setpoint(self):        return float(self.query("TEC:SET?"))

    def output(self, on):
        """Enable/disable TEC current. NOTE: gated by the rear Remote Enable
        input (DB-9 pin 1). Default polarity is ENABLE-HI with a pull-up, so it
        works unwired; if it won't enable, check remote_enable_polarity()."""
        self.command(f"TEC:OUTput {1 if on else 0}")

    def output_enabled(self):      return self.query("TEC:OUTput?") == "1"

    def set_step_size(self, deg):  self.command(f"TEC:STEP {int(deg / 0.01)}")  # step in degrees
    def get_step_size(self):       return int(self.query("TEC:STEP?")) * 0.01
    def step_up(self, n=1):        self.command(f"TEC:INC {n}")
    def step_down(self, n=1):      self.command(f"TEC:DEC {n}")

    # ================================================================== #
    #  Limits, tolerance, calibration
    # ================================================================== #
    def set_current_limit_pos(self, a): self.command(f"TEC:LIMit:IPOS {a}")
    def get_current_limit_pos(self):    return float(self.query("TEC:LIMit:IPOS?"))
    def set_current_limit_neg(self, a): self.command(f"TEC:LIMit:INEG {a}")   # enter as POSITIVE number
    def get_current_limit_neg(self):    return float(self.query("TEC:LIMit:INEG?"))
    def set_temp_limit_high(self, t):   self.command(f"TEC:LIMit:THI {t}")
    def get_temp_limit_high(self):      return float(self.query("TEC:LIMit:THI?"))
    def set_temp_limit_low(self, t):    self.command(f"TEC:LIMit:TLO {t}")
    def get_temp_limit_low(self):       return float(self.query("TEC:LIMit:TLO?"))
    def set_resistance_limit_high(self, r): self.command(f"TEC:LIMit:RHI {r}")
    def get_resistance_limit_high(self):    return float(self.query("TEC:LIMit:RHI?"))
    def set_resistance_limit_low(self, r):  self.command(f"TEC:LIMit:RLO {r}")
    def get_resistance_limit_low(self):     return float(self.query("TEC:LIMit:RLO?"))
    def set_voltage_limit(self, v):     self.command(f"TEC:VLIM {v}")
    def get_voltage_limit(self):        return float(self.query("TEC:VLIM?"))
    def set_cable_resistance(self, ohms): self.command(f"TEC:CABLER {ohms}")
    def get_cable_resistance(self):     return float(self.query("TEC:CABLER?"))

    def set_tolerance(self, deg, seconds): self.command(f"TEC:TOLerance {deg},{seconds}")
    def get_tolerance(self):            return [float(x) for x in self.query("TEC:TOLerance?").split(",")]

    # ================================================================== #
    #  PID & IntelliTune
    # ================================================================== #
    def set_pid(self, p, i=None, d=None):
        parts = [str(p)] + ([str(i)] if i is not None else []) + ([str(d)] if d is not None else [])
        self.command("TEC:PID " + ",".join(parts))

    def get_pid(self):                 return [float(x) for x in self.query("TEC:PID?").split(",")]
    def set_intellitune_mode(self, m): self.command(f"TEC:AUTOTUNE {m}")   # MANUAL/DISTURBANCE_REJECTION/SETPOINT_RESPONSE
    def get_intellitune_mode(self):    return int(self.query("TEC:AUTOTUNE?"))
    def intellitune_start(self):       self.command("TEC:TUNESTART")
    def intellitune_abort(self):       self.command("TEC:TUNEABORT")
    def intellitune_valid(self):       return self.query("TEC:VALID?") == "1"

    # ================================================================== #
    #  Signal polarities
    # ================================================================== #
    def set_remote_enable_polarity(self, level): self.command(f"TEC:INTPOL {level}")  # 1=ENABLE-HI (default)
    def remote_enable_polarity(self):  return self.query("TEC:INTSTAT?")
    def set_ld_shutdown_polarity(self, p): self.command(f"TEC:LDSHUTdown:POL {p}")
    def get_ld_shutdown_polarity(self):    return self.query("TEC:LDSHUTdown:POL?")

    # ================================================================== #
    #  Live readings
    # ================================================================== #
    def temperature(self):         return float(self.query("TEC:ACT?"))   # control sensor
    def aux_temperature(self):     return float(self.query("TEC:AUX?"))   # aux sensor, always C
    def tec_current(self):         return float(self.query("TEC:I?"))
    def tec_voltage(self):         return float(self.query("TEC:V?"))
    def condition(self):           return int(self.query("TEC:COND?"))    # live status bits
    def event(self):               return int(self.query("TEC:EVEnt?"))   # latched changes (read clears)

    # decoded condition-register flags
    def in_tolerance(self):        return bool(self.condition() & 512)
    def output_active(self):       return bool(self.condition() & 1024)
    def at_current_limit(self):    return bool(self.condition() & 1)
    def sensor_open(self):         return bool(self.condition() & 64)
    def sensor_shorted(self):      return bool(self.condition() & 32)
    def temp_high_limit_hit(self): return bool(self.condition() & 8)
    def temp_low_limit_hit(self):  return bool(self.condition() & 16)

    def status(self):
        """One-shot snapshot dict, handy for logging or publishing."""
        c = self.condition()
        return {
            "setpoint":       self.get_setpoint(),
            "actual":         self.temperature(),
            "tec_current":    self.tec_current(),
            "tec_voltage":    self.tec_voltage(),
            "in_tolerance":   bool(c & 512),
            "output_active":  bool(c & 1024),
            "current_limit":  bool(c & 1),
            "sensor_open":    bool(c & 64),
            "sensor_short":   bool(c & 32),
            "temp_hi_limit":  bool(c & 8),
            "temp_lo_limit":  bool(c & 16),
        }

    # ================================================================== #
    #  Profiles (save / recall full setups, 1-10; 0 = factory defaults)
    # ================================================================== #
    def save_profile(self, n):     self.command(f"*SAV {n}")
    def recall_profile(self, n):   self.command(f"*RCL {n}")
    def set_profile_name(self, n, line1, line2=""): self.command(f"PROFile:DESC {n},{line1},{line2}")
    def get_profile_name(self, n): return self.query(f"PROFile:DESC? {n}")

    # ================================================================== #
    #  Stored scripts (1-4). Commands inside a script are joined by '^'.
    # ================================================================== #
    def put_script(self, index, commands):
        """commands: list of command strings; stored (not run) at 1-4."""
        self.command(f"SCRIPT:PUT {index} " + "^".join(commands))

    def run_script(self, index):   self.command(f"SCRIPT:GO {index}")
    def get_script(self, index):   return self.query(f"SCRIPT:GET? {index}")


# --------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    resource = sys.argv[1] if len(sys.argv) > 1 else "TCPIP::192.168.1.50::INSTR"
    with TCLab(resource) as tc:
        print("Connected:", tc.idn)
        tc.set_units(TCLab.CELSIUS)
        print("Status:", tc.status())
        print("Errors:", tc.get_errors() or "clean")