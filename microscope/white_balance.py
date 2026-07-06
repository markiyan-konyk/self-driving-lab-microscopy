"""
Automatic white-balance calibration that respects the user's colour_gain multiplier.

The hardware AWB algorithm is run briefly, and the gains it converges on are read.
These gains are then scaled by the current colour_gain so that when the caller
stores them as red_gain and blue_gain (and later applies them via
apply_camera_controls), the effective gains (red_gain * colour_gain,
blue_gain * colour_gain) equal the AWB values.
"""

import time

def run_white_balance(picam2, current_colour_gain=1.0,
                      settle_frames=40, settle_timeout=8.0):
    """
    Run hardware AWB and return the red/blue gains that should be stored
    as fixed gains, taking the current colour_gain multiplier into account.

    Args:
        picam2: Picamera2 instance (must be running).
        current_colour_gain: The colour_gain value currently in effect
                             (from cam_controls["colour_gain"]).
        settle_frames: Number of frames to let AWB converge.
        settle_timeout: Maximum seconds to wait.

    Returns:
        (red_gain_to_store, blue_gain_to_store) or (None, None) on failure.
    """
    # Temporarily hand control to the ISP's AWB.
    picam2.set_controls({"AwbEnable": True, "AwbMode": 0})

    gains = None
    deadline = time.time() + settle_timeout

    try:
        # Let the algorithm converge and grab the most recent gains.
        for _ in range(settle_frames):
            if time.time() > deadline:
                break
            md = picam2.capture_metadata()
            g = md.get("ColourGains")
            if g is not None:
                gains = g
    finally:
        # Always re-freeze AWB so the gains we read are actually used.
        picam2.set_controls({"AwbEnable": False})

    if gains is None:
        return None, None

    raw_red, raw_blue = float(gains[0]), float(gains[1])

    # Compensate for the user's colour_gain multiplier.
    # We want: stored_red * colour_gain == raw_red  -> stored_red = raw_red / colour_gain
    # Same for blue.
    # Avoid division by zero.
    if current_colour_gain == 0.0:
        current_colour_gain = 1.0

    red_to_store = raw_red / current_colour_gain
    blue_to_store = raw_blue / current_colour_gain

    print(f"[white_balance] AWB raw gains: R={raw_red:.2f} B={raw_blue:.2f}")
    print(f"[white_balance] Stored gains (compensated for colour_gain={current_colour_gain:.2f}): R={red_to_store:.2f} B={blue_to_store:.2f}")

    return round(red_to_store, 2), round(blue_to_store, 2)
