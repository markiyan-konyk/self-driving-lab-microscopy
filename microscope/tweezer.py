"""Optical-tweezer geometry: where the laser lands, in image and global coords.

The laser is rigid to the objective, so the laser's global position is the
microscope's global (stage) position plus a galvo-induced offset:

    laser_global = microscope_global  +  galvo_offset(volts - home_volts)

This module owns that mapping plus the manual "zeroing" of the galvos. It is a
thin state+math module (same style as controls.py): main.py / client.py read
and write its module globals through the namespace.

Two calibrations live here, both PLACEHOLDERS until measured:

  * PIXELS_PER_VOLT  - the empirical, directly-measured map from command volts
    to image pixels (produced by galvo_tests/03_precision.py). This is the
    primary, verifiable quantity and drives the laser's IMAGE position.
  * VOLTS_TO_ANGLE / OPTICAL_THROW_UM / STAGE_STEPS_PER_UM - the physical
    decomposition (mirror deflection angle per volt, optical throw to the
    sample plane, stage steps per micrometre). These give the laser's GLOBAL
    position in micrometres for jump-scanning. The computation is wired now so
    only the numbers change once the hardware is calibrated.
"""

import math
import threading

# Serializes galvo command writes (sibling of controls.move_lock for the stage).
galvo_move_lock = threading.Lock()

# Native camera frame size; image centre is where the operator aims the laser
# when zeroing (the UI crosshair).
NATIVE_W, NATIVE_H = 640, 480
IMAGE_CENTRE_PX = {"x": NATIVE_W / 2.0, "y": NATIVE_H / 2.0}   # refined by 03_precision

# ---- Empirical volts -> pixels (PLACEHOLDER until 03_precision.py) ----
# [px_x, px_y] = PIXELS_PER_VOLT @ [vx - home_x, vy - home_y]
# Rows are pixels-per-volt; off-diagonals capture galvo/camera axis rotation.
PIXELS_PER_VOLT = ((100.0, 0.0),
                   (0.0, 100.0))

# ---- Physical decomposition (PLACEHOLDER) ----
VOLTS_TO_ANGLE = {"x": 0.05, "y": 0.05}     # radians of mirror deflection per volt
OPTICAL_THROW_UM = 50000.0                  # galvo-to-sample-plane optical distance (um)
STAGE_STEPS_PER_UM = {"x": 1.0, "y": 1.0, "z": 1.0}   # Sangaboard steps per micrometre

# ---- Home (zeroed) command volts: set by "zero out tweezers" ----
home_volts = {"x": 0.0, "y": 0.0}

# Default per-click jog size for the UI laser pad, in volts (kept small for
# untested mirrors; adjustable from the UI).
jog_volts = 0.05


def set_home(vx, vy):
    """Make the current command volts the laser's home (offset 0). The operator
    centres the spot on the crosshair, then calls this."""
    home_volts["x"] = float(vx)
    home_volts["y"] = float(vy)


def set_jog_volts(value):
    global jog_volts
    jog_volts = max(0.0001, float(value))
    return jog_volts


def galvo_offset_px(vx, vy):
    """Laser offset from image centre, in native pixels, for command volts."""
    ex = vx - home_volts["x"]
    ey = vy - home_volts["y"]
    (a, b), (c, d) = PIXELS_PER_VOLT
    return {"x": a * ex + b * ey, "y": c * ex + d * ey}


def laser_image_px(vx, vy):
    """Laser spot position in native image pixels (centre + galvo offset)."""
    off = galvo_offset_px(vx, vy)
    return {"x": IMAGE_CENTRE_PX["x"] + off["x"], "y": IMAGE_CENTRE_PX["y"] + off["y"]}


def _deflection_um(vx, vy):
    """Sample-plane displacement (um) from command volts, via the physical
    chain. Mirror deflection doubles the beam angle, hence the factor of 2."""
    ax = 2.0 * VOLTS_TO_ANGLE["x"] * (vx - home_volts["x"])
    ay = 2.0 * VOLTS_TO_ANGLE["y"] * (vy - home_volts["y"])
    return OPTICAL_THROW_UM * math.tan(ax), OPTICAL_THROW_UM * math.tan(ay)


def laser_global_um(stage_position, vx, vy):
    """Unified global coordinate (um) of the laser spot: the stage position
    (converted from steps) plus the galvo deflection. Stage<->galvo axis
    alignment is assumed identity here (PLACEHOLDER -- the camera is mounted 90
    degrees to the stage, so this needs the real rotation once measured). Z has
    no galvo contribution."""
    dx_um, dy_um = _deflection_um(vx, vy)
    sx = stage_position.get("x", 0) / STAGE_STEPS_PER_UM["x"]
    sy = stage_position.get("y", 0) / STAGE_STEPS_PER_UM["y"]
    sz = stage_position.get("z", 0) / STAGE_STEPS_PER_UM["z"]
    return {"x": sx + dx_um, "y": sy + dy_um, "z": sz}


def _round_pt(pt, n=1):
    return {k: round(v, n) for k, v in pt.items()}


def state(stage_position, galvo):
    """Telemetry block for the laser/tweezer. ``galvo`` may be None (no AWG)."""
    if galvo is None:
        return {
            "connected": False,
            "vx": None, "vy": None,
            "home": dict(home_volts),
            "jog_volts": jog_volts,
            "image_px": None,
            "global_um": None,
        }
    vx, vy = galvo.vx, galvo.vy
    return {
        "connected": True,
        "vx": round(vx, 4), "vy": round(vy, 4),
        "home": dict(home_volts),
        "jog_volts": jog_volts,
        "image_px": _round_pt(laser_image_px(vx, vy)),
        "global_um": _round_pt(laser_global_um(stage_position, vx, vy)),
    }
