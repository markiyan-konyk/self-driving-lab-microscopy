"""Automatic white-balance calibration.

Used by ``camera.run_white_balance_thread``, which calls this with the camera
*stopped* (but still configured). The routine restarts the camera, then
iteratively adjusts the red/blue colour gains until the red and blue channel
means match the green channel (grey-world balance over the near-white
pixels), stops the camera again and returns the final ``(red_gain,
blue_gain)`` for the caller to store in ``cam_controls``.

``target_white_level`` is the expected white level of the illuminated
background expressed as a 10-bit value (876/1023 ~ 219/255); it selects which
pixels count as "white" for the balance and excludes clipped ones.
"""

import time

import cv2
import numpy as np


def _to_bgr(frame):
    """Best-effort conversion of a capture_array() result to BGR."""
    if frame is None:
        raise RuntimeError("Camera returned no frame")
    if frame.ndim == 2:  # YUV420 planar: shape (height * 3/2, width)
        return cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
    if frame.ndim == 3 and frame.shape[2] == 4:  # XBGR8888 / XRGB8888
        return frame[:, :, :3]
    return frame


def _channel_means(bgr, target8):
    """Mean R, G, B over the calibration pixels.

    Uses pixels whose green level is in the near-white band (above half the
    target, below clipping) so the illuminated background drives the balance;
    falls back to the whole frame if fewer than 1% of pixels qualify.
    """
    green = bgr[:, :, 1]
    band = (green > target8 * 0.5) & (green < 250)
    if np.count_nonzero(band) < band.size // 100:
        band = np.ones_like(green, dtype=bool)
    sel = bgr[band].astype(np.float64)
    b_mean, g_mean, r_mean = sel.mean(axis=0)
    return r_mean, g_mean, b_mean


def run_white_balance(picam2, target_white_level=876, max_iterations=8,
                      tolerance=0.02, settle_time=0.3):
    """Calibrate red/blue gains on a (stopped) Picamera2. Returns (red, blue)."""
    target8 = target_white_level / 1023.0 * 255.0
    red_gain, blue_gain = 1.0, 1.0

    picam2.start()
    try:
        for _ in range(max_iterations):
            picam2.set_controls({
                "AwbEnable": False,
                "AeEnable": False,
                "ColourGains": (red_gain, blue_gain),
            })
            time.sleep(settle_time)

            bgr = _to_bgr(picam2.capture_array("main"))
            r_mean, g_mean, b_mean = _channel_means(bgr, target8)
            if r_mean <= 0 or g_mean <= 0 or b_mean <= 0:
                raise RuntimeError("White balance: frame too dark to calibrate")

            r_err = g_mean / r_mean
            b_err = g_mean / b_mean
            if abs(r_err - 1.0) < tolerance and abs(b_err - 1.0) < tolerance:
                break

            # ColourGains valid range on the Pi ISP; also matches the UI sliders.
            red_gain = float(np.clip(red_gain * r_err, 0.1, 8.0))
            blue_gain = float(np.clip(blue_gain * b_err, 0.1, 8.0))
    finally:
        picam2.stop()  # leave the camera as we found it; caller restarts it

    return round(red_gain, 2), round(blue_gain, 2)
