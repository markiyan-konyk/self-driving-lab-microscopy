"""Stage autofocus routines.

Used by ``camera.run_autofocus_thread``: the stage is swept along Z while a
focus metric is computed on frames captured from the (running) camera, then
the stage is moved to the Z position that maximised the metric.

The ``stage`` argument is anything providing the small interface of
``controls.SangaboardWrapper``: a ``position`` dict property plus
``move_rel(delta)`` / ``move_abs(target)``. The ``camera`` argument is a
started ``Picamera2`` instance.

Two Raspberry Pi realities are handled here:
  * the camera pipeline keeps a couple of requests in flight, so frames
    captured right after a move were exposed before/during it -> flush them;
  * the stage has mechanical backlash, so every Z position (including the
    final move to the winner) is approached from below.
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


def _capture_fresh(camera, flush=2):
    """Capture a frame that was exposed after now, discarding in-flight ones."""
    for _ in range(flush):
        camera.capture_array("main")
    return camera.capture_array("main")


def focus_score(camera, metric="jpeg_size"):
    """Single focus measurement on the camera's main stream.

    ``jpeg_size``: size of the JPEG-compressed frame. Sharp images carry more
    high-frequency detail and compress to bigger files, so bigger is sharper.
    ``laplacian``: variance of the Laplacian of the grayscale frame.
    """
    bgr = _to_bgr(_capture_fresh(camera))
    if metric == "laplacian":
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return float(len(buf))


def _goto_z(stage, z):
    pos = stage.position
    stage.move_abs({"x": pos["x"], "y": pos["y"], "z": int(z)})


def _goto_z_from_below(stage, z, backlash, settle_time):
    """Approach ``z`` moving upward so backlash is taken up consistently."""
    _goto_z(stage, z - backlash)
    _goto_z(stage, z)
    time.sleep(settle_time)


def _scan_z(stage, camera, z_positions, metric, settle_time, backlash):
    """Measure focus at each Z (ascending), every position approached from
    below so backlash affects all measurements identically."""
    _goto_z(stage, z_positions[0] - backlash)  # take up the slack first
    scores = []
    for z in z_positions:
        _goto_z(stage, z)
        time.sleep(settle_time)
        scores.append(focus_score(camera, metric))
    return scores


def fast_autofocus(stage, camera, dz=2000, n_steps=20, metric="jpeg_size",
                   settle_time=0.05, backlash=256):
    """Single sweep: scan [z - dz, z + dz] in ``n_steps`` and move to the
    best Z. Returns the chosen Z position."""
    centre = stage.position["z"]
    z_positions = np.linspace(centre - dz, centre + dz, n_steps).astype(int)
    scores = _scan_z(stage, camera, z_positions, metric, settle_time, backlash)
    best_z = int(z_positions[int(np.argmax(scores))])
    _goto_z_from_below(stage, best_z, backlash, settle_time)
    return best_z


def looping_autofocus(stage, camera, dz=2000, n_steps=20, metric="jpeg_size",
                      max_attempts=3, settle_time=0.05, backlash=256):
    """Repeated sweeps, re-centred on the best Z found each time.

    If the best score lands on an edge of the scanned range, the true focus
    is probably outside it, so another sweep is run from the new position
    (up to ``max_attempts``). A peak in the interior means we bracketed the
    focus and can stop. Returns the final Z position.
    """
    best_z = stage.position["z"]
    for _ in range(max_attempts):
        centre = stage.position["z"]
        z_positions = np.linspace(centre - dz, centre + dz, n_steps).astype(int)
        scores = _scan_z(stage, camera, z_positions, metric, settle_time, backlash)
        best_idx = int(np.argmax(scores))
        best_z = int(z_positions[best_idx])
        _goto_z_from_below(stage, best_z, backlash, settle_time)
        if 0 < best_idx < len(z_positions) - 1:
            break  # peak inside the scanned range -> focus bracketed
    return best_z
