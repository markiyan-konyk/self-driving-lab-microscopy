"""Client-side vision: assess a field of view cheaply from the live stream.

This is the fast/efficient path the user asked for -- a SINGLE-frame trackpy
detect with the auto-tuned percentile (the one efficiency knob) and workers=1,
NOT the full multi-core recording pipeline. It answers the only two questions
that decide whether a FOV is worth recording:

    * how many beads are on screen (more = better statistics), and
    * how clumped they are (touching beads break the single-particle MSD).

Plus a focus proxy (variance of Laplacian) and the measured stream fps. The Pi's
own tracker_node is idle (no trackpy in the ROS image), so this all runs here on
frames pulled from scope.stream_frames().
"""

import os
import time

import cv2
import numpy as np

from scopio_client import ScopioError

# Ensure viscosity/ is importable + BLAS threads pinned, THEN import trackpy.
from .. import _viscosity_import as _vi
import trackpy as tp

from ..context import Context

CLUMP_SIZE_FACTOR = 1.6      # tracker_node.py heuristic: size > 1.6*median => clump
_DIAMETER = _vi.DEFAULTS["diameter"]
_MINMASS = _vi.DEFAULTS["minmass"]
_CHANNEL = _vi.DEFAULTS["channel"]
_INVERT = _vi.DEFAULTS["invert"]


def grab_frames(scope, n, max_wait_s=20.0):
    """Pull up to n JPEG frames from the live stream; return (frames, mono_times).

    Closes the stream generator afterwards so we don't leak the HTTP connection.
    """
    frames, times = [], []
    gen = scope.stream_frames()
    t_deadline = time.monotonic() + max_wait_s
    try:
        for jpeg in gen:
            frames.append(jpeg)
            times.append(time.monotonic())
            if len(frames) >= n or time.monotonic() > t_deadline:
                break
    finally:
        close = getattr(gen, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass
    return frames, times


def decode(jpeg):
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def _mean_nn_dist(x, y):
    """Mean nearest-neighbour distance among detected features (px)."""
    if len(x) < 2:
        return None
    pts = np.column_stack([x, y]).astype(float)
    d = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(d, np.inf)
    return float(np.mean(d.min(axis=1)))


def assess_scene(ctx: Context, n_frames=5):
    """Cheap single-frame assessment of the current FOV. Returns a SceneMetrics dict."""
    scope = ctx.scope
    try:
        frames, times = grab_frames(scope, n_frames)
    except ScopioError as e:
        ctx.nb.error(f"assess_scene: stream unavailable ({e})")
        return {"bead_count": 0, "clump_fraction": 0.0, "focus_score": 0.0,
                "measured_fps": 0.0, "mean_nn_dist_px": None,
                "percentile": None, "stage_pos": _pos(scope), "snapshot": None,
                "error": str(e)}
    if not frames:
        ctx.nb.error("assess_scene: no frames received")
        return {"bead_count": 0, "clump_fraction": 0.0, "focus_score": 0.0,
                "measured_fps": 0.0, "mean_nn_dist_px": None, "percentile": None,
                "stage_pos": _pos(scope), "snapshot": None}

    bgr = decode(frames[-1])
    plane = bgr[:, :, _CHANNEL]

    tp.quiet()
    pct = _vi.choose_percentile(plane, _DIAMETER, _MINMASS, _INVERT)
    feats = tp.locate(plane, diameter=_DIAMETER, minmass=_MINMASS,
                      invert=_INVERT, percentile=pct)

    n = len(feats)
    if n:
        sizes = feats["size"].to_numpy(dtype=float)
        med = float(np.median(sizes))
        clump_fraction = float(np.mean(sizes > CLUMP_SIZE_FACTOR * med)) if med > 0 else 0.0
        nn = _mean_nn_dist(feats["x"].to_numpy(), feats["y"].to_numpy())
    else:
        clump_fraction, nn = 0.0, None

    focus = float(cv2.Laplacian(plane, cv2.CV_64F).var())

    if len(times) >= 2 and times[-1] > times[0]:
        measured_fps = (len(times) - 1) / (times[-1] - times[0])
    else:
        measured_fps = 0.0

    snap = _save_snapshot(ctx, bgr, feats)

    scene = {
        "bead_count": int(n),
        "clump_fraction": round(clump_fraction, 3),
        "focus_score": round(focus, 1),
        "measured_fps": round(float(measured_fps), 2),
        "mean_nn_dist_px": round(nn, 1) if nn is not None else None,
        "percentile": round(float(pct), 1),
        "stage_pos": _pos(scope),
        "snapshot": snap,
    }
    ctx.nb.metric(
        f"scene: {n} beads, clump {clump_fraction:.0%}, focus {focus:.0f}, "
        f"{measured_fps:.1f} fps", scene)
    return scene


def _save_snapshot(ctx: Context, bgr, feats):
    """Annotate the frame with detected beads and save a preview JPEG."""
    canvas = bgr.copy()
    r = max(3, _DIAMETER // 2)
    for _, row in feats.iterrows():
        x, y = int(round(row["x"])), int(round(row["y"]))
        cv2.circle(canvas, (x, y), r, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(canvas, f"beads {len(feats)}", (8, 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
    snap_dir = os.path.join(ctx.run_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    path = os.path.join(snap_dir, f"{ctx.next_scene_id()}.jpg")
    cv2.imwrite(path, canvas)
    ctx.nb.snapshot(path, f"{len(feats)} beads")
    return os.path.relpath(path, ctx.run_dir).replace(os.sep, "/")


def _pos(scope):
    try:
        p = scope.stage.position()
        if p:
            return {"x": p.get("x"), "y": p.get("y"), "z": p.get("z")}
    except ScopioError:
        pass
    return None
