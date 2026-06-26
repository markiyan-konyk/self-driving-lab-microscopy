"""Client-side galvo geometry + SCPI composition for the ROS passthrough.

The ROS ``galvo_node`` is a *dumb* VISA passthrough: it relays command strings
and knows nothing about volts, pixels, or waveforms (see DECISIONS.md). All of
that meaning lives HERE, in client code, so that swapping the AWG never touches
ROS.

A ``GalvoClient`` instance:
  * tracks the last commanded volts and the "home" (zeroed) position,
  * turns high-level intents (point, nudge, zero, on/off, sine, dc) into the
    DG1022Z SCPI strings to send via the ``awg/write`` service,
  * computes where the laser lands in image pixels and global micrometres
    (ported from the old microscope/tweezer.py geometry).

The string builders return a *list* of SCPI commands; the caller sends each one
through ``awg/write``. This module imports no hardware libraries, so the UI and
any external galvo app can reuse it. To target a different instrument, write a
sibling class with the same method names and different SCPI.

Calibration constants are PLACEHOLDERS until galvo_tests/03_precision.py
measures them; only the numbers change.
"""

import math

V_MIN, V_MAX = -5.0, 5.0

# Native camera frame size; image centre is where the operator aims when zeroing.
NATIVE_W, NATIVE_H = 640, 480
IMAGE_CENTRE_PX = {"x": NATIVE_W / 2.0, "y": NATIVE_H / 2.0}

# [px_x, px_y] = PIXELS_PER_VOLT @ [vx - home_x, vy - home_y]   (PLACEHOLDER)
PIXELS_PER_VOLT = ((100.0, 0.0), (0.0, 100.0))
# Physical decomposition for global micrometres (PLACEHOLDER)
VOLTS_TO_ANGLE = {"x": 0.05, "y": 0.05}   # rad of mirror deflection per volt
OPTICAL_THROW_UM = 50000.0                # galvo-to-sample optical distance (um)


def clamp(v, lo=V_MIN, hi=V_MAX):
    return max(lo, min(hi, float(v)))


class GalvoClient:
    """Stateful helper that builds SCPI strings for a DG1022Z driving a 2-axis
    galvo (CH1 = X mirror, CH2 = Y mirror)."""

    def __init__(self, jog_volts=0.05):
        self.vx = 0.0
        self.vy = 0.0
        self.home = {"x": 0.0, "y": 0.0}
        self.jog_volts = max(0.0001, float(jog_volts))

    # ---- SCPI builders (return lists of command strings) ---- #
    def init_cmds(self):
        """Put both channels in DC mode at 0 V with High-Z output enabled."""
        cmds = []
        for ch in (1, 2):
            cmds.append(f":OUTPut{ch}:IMPedance INFinity")
            cmds.append(f":SOURce{ch}:APPLy:DC 1,1,0")
            cmds.append(f":OUTPut{ch} ON")
        cmds += self.set_volts_cmds(0.0, 0.0)
        return cmds

    def set_volts_cmds(self, vx, vy):
        """Absolute DC pointing (clamped). Updates tracked volts."""
        self.vx = clamp(vx)
        self.vy = clamp(vy)
        return [f":SOURce1:VOLTage:OFFSet {self.vx:.4f}",
                f":SOURce2:VOLTage:OFFSet {self.vy:.4f}"]

    def nudge_cmds(self, dvx=0.0, dvy=0.0):
        return self.set_volts_cmds(self.vx + dvx, self.vy + dvy)

    def jog_cmds(self, direction):
        """Convenience: nudge by one jog step in a screen direction."""
        j = self.jog_volts
        deltas = {"up": (0, j), "down": (0, -j), "left": (-j, 0), "right": (j, 0)}
        dvx, dvy = deltas.get(direction, (0, 0))
        return self.nudge_cmds(dvx, dvy)

    def point_mode_cmds(self):
        """Return both channels to DC-offset (pointing) mode and re-apply volts."""
        cmds = [f":SOURce{ch}:APPLy:DC 1,1,0" for ch in (1, 2)]
        return cmds + self.set_volts_cmds(self.vx, self.vy)

    def sine_cmds(self, channel, freq_hz, amplitude_vpp, offset_v=0.0):
        return [f":SOURce{channel}:APPLy:SINusoid {freq_hz},{amplitude_vpp},{clamp(offset_v)}"]

    def dc_cmds(self, channel, offset_v):
        offset_v = clamp(offset_v)
        if channel == 1:
            self.vx = offset_v
        elif channel == 2:
            self.vy = offset_v
        return [f":SOURce{channel}:APPLy:DC 1,1,{offset_v}"]

    def output_cmds(self, on):
        state = "ON" if on else "OFF"
        return [f":OUTPut{ch} {state}" for ch in (1, 2)]

    # ---- Zeroing ---- #
    def set_home(self, vx=None, vy=None):
        self.home["x"] = float(self.vx if vx is None else vx)
        self.home["y"] = float(self.vy if vy is None else vy)

    def set_jog_volts(self, value):
        self.jog_volts = max(0.0001, float(value))
        return self.jog_volts

    # ---- Geometry (volts -> pixels / micrometres) ---- #
    def offset_px(self):
        ex, ey = self.vx - self.home["x"], self.vy - self.home["y"]
        (a, b), (c, d) = PIXELS_PER_VOLT
        return {"x": a * ex + b * ey, "y": c * ex + d * ey}

    def image_px(self):
        off = self.offset_px()
        return {"x": IMAGE_CENTRE_PX["x"] + off["x"], "y": IMAGE_CENTRE_PX["y"] + off["y"]}

    def global_um(self, stage_um):
        """Global laser position (um): stage position (um) + galvo deflection."""
        ax = 2.0 * VOLTS_TO_ANGLE["x"] * (self.vx - self.home["x"])
        ay = 2.0 * VOLTS_TO_ANGLE["y"] * (self.vy - self.home["y"])
        dx = OPTICAL_THROW_UM * math.tan(ax)
        dy = OPTICAL_THROW_UM * math.tan(ay)
        return {"x": stage_um.get("x", 0.0) + dx,
                "y": stage_um.get("y", 0.0) + dy,
                "z": stage_um.get("z", 0.0)}

    def state(self):
        return {"vx": round(self.vx, 4), "vy": round(self.vy, 4),
                "home": dict(self.home), "jog_volts": self.jog_volts,
                "image_px": {k: round(v, 1) for k, v in self.image_px().items()}}
