"""Real-timestamp clip capture.

The manual pipeline fabricates ``timestamp_ms = frame / fps * 1000`` from a
nominal fps, and the UI recorder samples the latest frame on a sleep timer --
neither reflects true frame timing, which biases the diffusion coefficient. Here
we instead stamp every frame with its real arrival time (time.monotonic()) as it
comes off the stream, and persist those timestamps alongside the frames. The
downstream MSD uses tau = mean(t[lag:] - t[:-lag]), which is exact even for the
irregular (jittery) intervals we actually get -- so real timing flows straight
through the existing physics.
"""

import csv
import json
import os
import time

import numpy as np

from scopio_client import ScopioError

from ..context import Context


def record_clip(ctx: Context, duration_s: float, stage_pos=None):
    """Capture a clip, saving frames + real timestamps. Returns a ClipRecord dict."""
    lo, hi = ctx.cfg.clip_bounds_s
    duration_s = float(max(lo, min(hi, duration_s)))
    clip_id = ctx.next_clip_id()
    clip_dir = os.path.join(ctx.run_dir, "clips", clip_id)
    frames_dir = os.path.join(clip_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    target_fps = ctx.cfg.target_fps
    frame_cap = int(duration_s * target_fps * 3) + 100     # runaway guard
    wall_deadline = time.monotonic() + duration_s + 8.0

    ctx.nb.phase(f"recording {clip_id} ({duration_s:g}s)",
                 clip_id=clip_id, duration_s=duration_s)

    times = []
    gen = ctx.scope.stream_frames()
    t0 = None
    err = None
    try:
        for jpeg in gen:
            now = time.monotonic()
            if t0 is None:
                t0 = now
            idx = len(times)
            with open(os.path.join(frames_dir, f"frame_{idx:06d}.jpg"), "wb") as f:
                f.write(jpeg)
            times.append(now)
            if (now - t0) >= duration_s or len(times) >= frame_cap or now > wall_deadline:
                break
    except ScopioError as e:
        err = str(e)
        ctx.nb.error(f"record_clip: stream error after {len(times)} frames ({e})")
    finally:
        close = getattr(gen, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    n = len(times)
    if n < 2:
        rec = {"clip_id": clip_id, "dir": _rel(ctx, clip_dir), "duration_s": 0.0,
               "n_frames": n, "measured_fps": 0.0, "fps_jitter_pct": 0.0,
               "stage_pos": stage_pos, "csv_path": None, "error": err or "too few frames"}
        ctx.nb.error(f"{clip_id}: only {n} frames captured", **rec)
        return rec

    t = np.array(times) - times[0]
    ts_ms = t * 1000.0
    dt = np.diff(t)
    measured_fps = (n - 1) / (t[-1] - t[0]) if t[-1] > t[0] else 0.0
    jitter_pct = float(np.std(dt) / np.mean(dt) * 100.0) if np.mean(dt) > 0 else 0.0

    # persist real timestamps next to the frames
    ts_csv = os.path.join(clip_dir, "timestamps.csv")
    with open(ts_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["frame", "timestamp_ms"])
        for i, ms in enumerate(ts_ms):
            w.writerow([i, f"{ms:.3f}"])

    meta = {
        "clip_id": clip_id, "duration_s": round(float(t[-1]), 3), "n_frames": n,
        "measured_fps": round(float(measured_fps), 3),
        "fps_jitter_pct": round(jitter_pct, 2),
        "target_fps": target_fps, "stage_pos": stage_pos,
    }
    with open(os.path.join(clip_dir, "clip_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    rec = {"clip_id": clip_id, "dir": _rel(ctx, clip_dir),
           "duration_s": meta["duration_s"], "n_frames": n,
           "measured_fps": meta["measured_fps"], "fps_jitter_pct": jitter_pct,
           "stage_pos": stage_pos, "csv_path": None}
    ctx.nb.result(
        f"{clip_id}: {n} frames, {measured_fps:.1f} fps (jitter {jitter_pct:.1f}%)",
        **rec)
    return rec


def _rel(ctx, path):
    return os.path.relpath(path, ctx.run_dir).replace(os.sep, "/")
