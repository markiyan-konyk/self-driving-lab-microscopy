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

# ABSOLUTE hardware ceiling: the galvo driver tolerates +/-15 V bipolar on its
# command input. The DG1022Z can swing to ~+/-10 V (High-Z, up to 20 Vpp), so
# this is the "never exceed, or you cook the mirrors" backstop. Nothing in this
# module is allowed to command an instantaneous voltage outside +/-V_LIMIT.
V_LIMIT = 15.0

# Conservative *working* envelope for normal pointing/amplitudes. Deliberately
# tighter than V_LIMIT until galvo_tests/03_precision.py establishes the real
# mechanical range. Widen toward V_LIMIT once the hardware limits are known --
# it can never be widened past V_LIMIT (clamp() enforces that).
V_MIN, V_MAX = -5.0, 5.0


def clamp(v, lo=V_MIN, hi=V_MAX):
    """Clamp a commanded voltage into the working envelope, never exceeding the
    absolute +/-V_LIMIT hardware ceiling even if lo/hi are widened."""
    lo = max(lo, -V_LIMIT)
    hi = min(hi, V_LIMIT)
    return max(lo, min(hi, float(v)))


def clamp_sine(amplitude_vpp, offset_v, lo=V_MIN, hi=V_MAX):
    """Clamp a sine's (amplitude_vpp, offset) so the *instantaneous* voltage
    -- which peaks at ``offset +/- amplitude_vpp/2`` -- stays within [lo, hi]
    and inside the absolute +/-V_LIMIT ceiling. Returns the safe (amp, offset).

    This is the gap that a bare offset-clamp misses: a small/zero offset with a
    huge amplitude would still drive the mirror past its limit on the peaks.
    """
    lo = max(lo, -V_LIMIT)
    hi = min(hi, V_LIMIT)
    offset_v = max(lo, min(hi, float(offset_v)))
    amp = max(0.0, float(amplitude_vpp))
    # Peak excursion above/below the offset is amp/2; cap it to the nearer rail.
    headroom_vpp = 2.0 * min(hi - offset_v, offset_v - lo)
    return min(amp, headroom_vpp), offset_v


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
    def apply_sine(self, channel, freq_hz, amplitude_vpp, offset_v=0.0,
                   phase_deg=0.0):
        """Drive one axis with a continuous sine. Used for circles/Lissajous
        and as the template for Phase-2 'functions'.

        BOTH amplitude and offset are clamped together (see ``clamp_sine``) so
        the instantaneous peak can never exceed the galvo's voltage limit.

        ``phase_deg`` sets the start phase. For a circle, drive both axes at the
        same freq/amp with CH1 at 0 deg and CH2 at 90 deg, then call
        ``sync_phase()`` so the offset actually holds between the channels.
        """
        amplitude_vpp, offset_v = clamp_sine(amplitude_vpp, offset_v)
        self.awg.write(
            f":SOURce{channel}:APPLy:SINusoid {freq_hz},{amplitude_vpp},{offset_v}"
        )
        # APPLy resets phase to 0, so set it afterwards.
        self.awg.write(f":SOURce{channel}:PHASe {float(phase_deg)}")

    def apply_ramp(self, channel, freq_hz, amplitude_vpp, offset_v=0.0,
                   symmetry_pct=100.0):
        """Drive one axis with a ramp/sawtooth. With a ramp on X and a sine on
        Y you trace an actual sine *curve* on the screen. ``symmetry_pct`` is
        100 for a rising sawtooth (fast flyback), 50 for a triangle."""
        amplitude_vpp, offset_v = clamp_sine(amplitude_vpp, offset_v)
        self.awg.write(
            f":SOURce{channel}:APPLy:RAMP {freq_hz},{amplitude_vpp},{offset_v}"
        )
        self.awg.write(f":SOURce{channel}:FUNCtion:RAMP:SYMMetry {float(symmetry_pct)}")

    def sync_phase(self):
        """Re-align CH1/CH2 start phases so a programmed phase offset (e.g. the
        90 deg that turns two equal sines into a circle) actually holds. The
        two channels otherwise free-run independently."""
        self.awg.write(":SOURce1:PHASe:SYNChronize")

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
