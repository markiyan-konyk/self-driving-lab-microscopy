"""Two-axis galvo mirror pair driven by a Rigol DG1022Z AWG over VISA.

CH1 = X mirror, CH2 = Y mirror. The mirrors are *pointed* by writing a DC
offset voltage into each galvo driver. For prolonged motion (the optical-trap
"functions" that Phase 2 will expose as ROS actions) the AWG is told to
*generate* a waveform itself, so the Pi sends one command instead of streaming
points (see ``apply_sine`` / ``apply_waveform``).

This class is a thin hardware driver: it knows volts, not pixels. The mapping
from command volts to where the laser lands in the image / in global space
lives in ``tweezer.py``.
"""

import pyvisa

# Safe commanded-voltage envelope for the galvo driver. These are deliberately
# conservative until galvo_tests/03_precision.py establishes the real
# mechanical range -- clamping here protects untested mirrors from a bad
# command. Widen once the hardware limits are known.
V_MIN, V_MAX = -5.0, 5.0


def clamp(v, lo=V_MIN, hi=V_MAX):
    """Clamp a commanded voltage into the safe envelope."""
    return max(lo, min(hi, float(v)))


class Galvo:
    """Driver for the DG1022Z driving a two-axis galvo. Tracks the last
    commanded volts so callers (UI, geometry) can read the current pointing."""

    def __init__(self, resource, timeout_ms=5000):
        rm = pyvisa.ResourceManager()
        self.awg = rm.open_resource(resource)
        self.awg.timeout = timeout_ms
        self.idn = self.awg.query("*IDN?").strip()      # confirm it's the DG1022Z
        print(f"Galvo connected: {self.idn}")

        # Last commanded volts (the mirrors hold these until changed).
        self.vx = 0.0
        self.vy = 0.0

        for ch in (1, 2):
            self.awg.write(f":OUTPut{ch}:IMPedance INFinity")  # High-Z into the driver
            self.awg.write(f":SOURce{ch}:APPLy:DC 1,1,0")      # DC mode, 0 V to start
            self.awg.write(f":OUTPut{ch} ON")
        self.set_volts(0.0, 0.0)

    # ------------------------------------------------------------------ #
    #  DC pointing
    # ------------------------------------------------------------------ #
    def set_volts(self, vx, vy):
        """Point the mirrors to absolute command volts (clamped). Returns the
        actually-applied (vx, vy)."""
        self.vx = clamp(vx)
        self.vy = clamp(vy)
        self.awg.write(f":SOURce1:VOLTage:OFFSet {self.vx:.4f}")
        self.awg.write(f":SOURce2:VOLTage:OFFSet {self.vy:.4f}")
        return self.vx, self.vy

    def nudge(self, dvx=0.0, dvy=0.0):
        """Move the mirrors by a relative delta in volts."""
        return self.set_volts(self.vx + dvx, self.vy + dvy)

    def get_volts(self):
        return {"vx": self.vx, "vy": self.vy}

    def point_mode(self):
        """Return both channels to DC-offset (pointing) mode, e.g. after a
        waveform. Re-applies the current offsets."""
        for ch in (1, 2):
            self.awg.write(f":SOURce{ch}:APPLy:DC 1,1,0")
        self.set_volts(self.vx, self.vy)

    # ------------------------------------------------------------------ #
    #  Prolonged waveforms (one long command -> the AWG generates motion)
    # ------------------------------------------------------------------ #
    def apply_sine(self, channel, freq_hz, amplitude_vpp, offset_v=0.0):
        """Drive one axis with a continuous sine. Used for circles/Lissajous
        and as the template for Phase-2 'functions'. Offset is clamped; keep
        amplitude within the driver's safe range."""
        offset_v = clamp(offset_v)
        self.awg.write(
            f":SOURce{channel}:APPLy:SINusoid {freq_hz},{amplitude_vpp},{offset_v}"
        )

    def apply_dc(self, channel, offset_v):
        """Put one channel in DC mode at a given offset."""
        offset_v = clamp(offset_v)
        self.awg.write(f":SOURce{channel}:APPLy:DC 1,1,{offset_v}")
        if channel == 1:
            self.vx = offset_v
        elif channel == 2:
            self.vy = offset_v

    # ------------------------------------------------------------------ #
    #  Teardown
    # ------------------------------------------------------------------ #
    def off(self):
        for ch in (1, 2):
            self.awg.write(f":OUTPut{ch} OFF")

    def close(self):
        try:
            self.awg.close()
        except Exception:
            pass
