"""Automatic white-balance calibration.

Used by ``camera.run_white_balance_thread``, which calls this with the camera
*running*. Instead of a hand-rolled grey-world loop (which could converge to a
wrong, bluish result and visibly "revert" after briefly looking correct), we
borrow the Raspberry Pi ISP's own, properly tuned auto-white-balance:

  1. enable ``AwbEnable`` so the hardware AWB algorithm runs,
  2. let it converge on the current scene over a number of frames,
  3. read the colour gains it settled on from the frame metadata,
  4. disable ``AwbEnable`` again and hand those gains back to the caller, who
     stores them in ``cam_controls`` and re-applies them as fixed gains.

The caller keeps the camera running before and after; this routine only
toggles the AWB control and reads metadata.
"""

import time


def run_white_balance(picam2, settle_frames=40, settle_timeout=8.0):
    """Run the hardware AWB briefly and return the ``(red_gain, blue_gain)``
    it converged on, or ``None`` if the camera did not report colour gains.
    """
    # Hand control to the ISP's auto white balance with auto exposure left off
    # (we keep the operator-chosen exposure so brightness does not jump).
    picam2.set_controls({"AwbEnable": True, "AwbMode": 0})

    try:
        gains = None
        deadline = time.time() + settle_timeout
        # Let the AWB algorithm settle, tracking the most recent gains it picks.
        for _ in range(settle_frames):
            if time.time() > deadline:
                break
            md = picam2.capture_metadata()
            g = md.get("ColourGains")
            if g is not None:
                gains = g
    finally:
        # Always re-freeze AWB so the gains we return actually stick.
        picam2.set_controls({"AwbEnable": False})

    if gains is None:
        return None
    red_gain, blue_gain = float(gains[0]), float(gains[1])
    print(f"[white_balance] AWB settled on R={red_gain:.2f} B={blue_gain:.2f}")
    return round(red_gain, 2), round(blue_gain, 2)
