"""
track_balls.py
==============
Detect and track silica (SiO2) microspheres in a microscope video using trackpy,
and write an annotated output video where every ball is circled, its centre marked,
and labelled with a persistent track ID.

Pipeline
--------
1.  Read every frame (OpenCV, BGR).
2.  Use one colour channel for detection (green by default -- it has the best
    contrast in this footage; the red channel is essentially empty).
3.  `trackpy.locate` on each frame  -> sub-pixel feature positions ("features").
4.  `trackpy.link`                  -> stitch features across frames into tracks,
                                       each given a stable integer `particle` id.
5.  Re-draw every frame: circle + centre dot + id, then encode to a video file.
6.  Write the full tracking table to CSV (one row per ball per frame).

Quick start
-----------
    python track_balls.py rec1.mp4 -o tracked.mp4
        -> writes tracked.mp4 (annotated video) and tracked.csv (tracking table)

Only the data, no video (faster):
    python track_balls.py rec1.mp4 --no-video --csv tracks.csv

Tune detection if balls are missed / spurious:
    python track_balls.py rec1.mp4 --diameter 11 --minmass 800

Fast preview of the first 60 frames only:
    python track_balls.py rec1.mp4 --max-frames 60

The CSV columns are: particle, frame, timestamp_ms, x, y, mass, size, ecc,
signal, raw_mass, ep.  `particle` is the id drawn on the video; (x, y) are
sub-pixel positions in pixels; `timestamp_ms` (= frame / fps * 1000) is the
real-time stamp the downstream viscosity calculation works in.

The defaults below are tuned for the supplied 640x480 / 60 fps clip.
"""

import argparse
import os
import time
import warnings

# Pin native math libraries to ONE thread each, BEFORE numpy / trackpy import. We
# parallelise detection across PROCESSES (see detect()); without this, every worker
# would also spawn one BLAS/OpenMP thread per core, so processes x threads would
# oversubscribe the CPU and bog the whole machine down. setdefault = overridable.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from concurrent.futures import ProcessPoolExecutor

import cv2
import numpy as np
import pandas as pd
import trackpy as tp

# trackpy warns (once per empty frame) "No maxima survived ... filtering" whenever a
# frame yields zero features -- harmless with our high auto-percentile (a bead briefly
# dips below threshold; linking 'memory' bridges the gap). Silence just that advisory so
# it doesn't spam the log; detect() still reports how many frames came back empty. This
# runs at import in the parent AND in every spawned worker, so parallel runs are quiet too.
warnings.filterwarnings("ignore", message="No maxima survived", category=UserWarning)


# --------------------------------------------------------------------------- #
#  Defaults (tuned for the 640x480 clip -- see the README notes at the bottom) #
# --------------------------------------------------------------------------- #
DEFAULTS = dict(
    diameter=11,      # apparent feature size in px; MUST be odd. Bigger video => bigger value.
    minmass=800,      # min integrated brightness to keep a feature (rejects noise).
    percentile="auto",# brightness percentile a local max must beat to be REFINED.
                      # "auto" (default) sizes it from how much of frame 1 the beads fill:
                      # a SPARSE clip -> high percentile (skips the noise trackpy would
                      # otherwise refine then throw away -> big speedup), a CROWDED clip ->
                      # low percentile (keeps every bead). Or set a number 0-100 to force
                      # it. See choose_percentile(). (minmass does NOT affect speed -- it
                      # filters AFTER the expensive refinement.)
    channel=1,        # OpenCV BGR channel used for detection: 0=B, 1=G, 2=R. Green wins here.
    invert=False,     # balls are BRIGHT on a darker background -> False. Flip if yours are dark.
    search_range=5,   # max px a ball may move between frames (measured ~0.8px max here).
    memory=3,         # frames a ball may vanish (e.g. behind a blur) and still keep its id.
    out_scale=2,      # upscale the OUTPUT video so the tiny id labels are readable.
    workers="auto",   # CPU processes for the (per-frame, parallel) detect step.
                      # "auto" = cores - 1 (leaves one core free so the machine stays
                      # usable); 1 = serial; or an int. Linking stays serial -- it's cheap
                      # and each frame depends on the previous one. Native threads are
                      # pinned to 1 per process (top of file) so processes x threads can't
                      # oversubscribe your CPU.
)


def load_frames(path):
    """Read all frames of a video into a list of BGR uint8 arrays + return fps."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Could not open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if not frames:
        raise RuntimeError("No frames decoded -- is the file a valid video?")
    return frames, fps


def choose_percentile(plane, diameter, minmass, invert,
                      headroom=3.0, p_min=50.0, p_max=96.0):
    """Pick a smart detection `percentile` from how much of the frame the beads fill.

    trackpy thresholds candidate maxima at the `percentile`-th percentile of the
    image, so the ideal value sits just above the background: if beads cover a
    fraction f of the frame, percentile ~ 100*(1 - f). We estimate f from a single
    detect on this frame (bead count x bead area / frame area), inflate it by
    `headroom` to leave room for a few new beads drifting into view, then clamp to
    a safe range. Sparse frame -> high percentile (fast); crowded frame -> low.
    """
    tp.quiet()
    n = len(tp.locate(plane, diameter=diameter, minmass=minmass, invert=invert))
    h, w = plane.shape[:2]
    bead_area = np.pi * (diameter / 2.0) ** 2
    occupancy = max(n, 1) * bead_area / float(h * w)
    pct = 100.0 * (1.0 - min(1.0, headroom * occupancy))
    pct = float(max(p_min, min(p_max, pct)))
    print(f"  [auto-percentile] {n} beads in frame 1, "
          f"~{occupancy * 100:.1f}% frame occupancy -> percentile {pct:.0f}")
    return pct


def _locate_one(task):
    """Detect one frame inside a worker process. Top-level so it stays picklable."""
    i, plane, diameter, minmass, invert, percentile = task
    tp.quiet()
    f = tp.locate(plane, diameter=diameter, minmass=minmass,
                  invert=invert, percentile=percentile)
    f["frame"] = i
    return f


def _resolve_workers(workers, n_tasks, frames_per_worker=250):
    """Turn workers ('auto' | int | None) into a safe process count.

    Spawning a process is expensive (Windows starts a fresh interpreter and
    re-imports trackpy/numba in every worker, ~1-2 s each), so for "auto" we cap
    the count by the WORKLOAD too: each worker should handle ~`frames_per_worker`
    frames or its spin-up costs more than it saves. Short clips therefore stay
    serial; only long videos fan out across cores. Always leaves one core free.
    """
    cpu = os.cpu_count() or 2
    if workers in ("auto", None):
        w = min(cpu - 1, max(1, n_tasks // frames_per_worker))
    else:
        w = int(workers)
    return max(1, min(w, cpu, n_tasks))    # never more than cores, or frames


def _print_progress(done, n, t0, last_count):
    elapsed = time.time() - t0
    rate = done / elapsed if elapsed > 0 else 0.0
    eta = (n - done) / rate if rate else 0.0
    print(f"  detect {done:4d}/{n}  "
          f"({rate:5.1f} frame/s, ETA {eta:4.1f}s, {last_count} balls this frame)")


def _finalize(all_feats, n):
    """Concatenate per-frame features; flag frames where nothing was detected."""
    # Drop empty frames before concat -- keeping them is a no-op data-wise but trips a
    # pandas FutureWarning about concatenating empty/all-NA entries.
    non_empty = [f for f in all_feats if len(f)]
    features = pd.concat(non_empty, ignore_index=True) if non_empty else all_feats[0]
    detected = features["frame"].nunique() if len(features) else 0
    empty = n - detected
    if empty:
        print(f"  note: {empty}/{n} frames had no beads pass the filter "
              f"(linking 'memory' bridges short gaps; lower percentile/minmass "
              f"if it's a large fraction)")
    return features


def _detect_serial(frames, channel, diameter, minmass, invert, percentile):
    """Single-process detection (used for small jobs or workers=1)."""
    all_feats = []
    n = len(frames)
    t0 = time.time()
    for i in range(n):
        f = tp.locate(frames[i][:, :, channel], diameter=diameter,
                      minmass=minmass, invert=invert, percentile=percentile)
        f["frame"] = i
        all_feats.append(f)
        if (i + 1) % 25 == 0 or i == n - 1:
            _print_progress(i + 1, n, t0, len(f))
    return _finalize(all_feats, n)


def detect(frames, channel, diameter, minmass, invert, percentile="auto",
           workers="auto"):
    """Run trackpy.locate on every frame. Returns one concatenated DataFrame.

    Detection is per-frame independent, so it is spread across CPU **processes**
    (`workers`); linking stays serial. `workers`: "auto" = cores-1, an int, or 1
    to force serial. `percentile`: "auto" sizes it from frame 1 (choose_percentile)
    or a number 0-100. Live throughput is printed as it runs.

    NOTE: because this uses multiprocessing, any script that calls it with
    workers > 1 MUST run its entry point under `if __name__ == "__main__":`
    (track.py's CLI, main.py and test_tracking.py already do).
    """
    tp.quiet()  # silence trackpy's own per-frame logging
    if percentile in ("auto", None):
        percentile = choose_percentile(frames[0][:, :, channel],
                                        diameter, minmass, invert)

    n = len(frames)
    n_workers = _resolve_workers(workers, n)

    # Small jobs aren't worth the process-pool spin-up -> just run serially.
    if n_workers == 1 or n < 64:
        return _detect_serial(frames, channel, diameter, minmass, invert, percentile)

    print(f"  detecting across {n_workers} processes (of {os.cpu_count()} cores) ...")
    tasks = ((i, frames[i][:, :, channel], diameter, minmass, invert, percentile)
             for i in range(n))
    chunksize = max(1, min(32, n // (n_workers * 4)))   # amortise the hand-off
    all_feats = []
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        for f in pool.map(_locate_one, tasks, chunksize=chunksize):
            all_feats.append(f)
            done += 1
            if done % 25 == 0 or done == n:
                _print_progress(done, n, t0, len(f))
    return _finalize(all_feats, n)


def link(features, search_range, memory):
    """Stitch per-frame features into tracks with a persistent `particle` id."""
    tp.quiet()
    tracks = tp.link(features, search_range=search_range, memory=memory)
    n_tracks = tracks["particle"].nunique()
    # How long does each track survive? (a sanity signal -- mostly-long tracks = good linking)
    lengths = tracks.groupby("particle").size()
    print(f"  linked into {n_tracks} tracks "
          f"(median length {lengths.median():.0f} / {features['frame'].max()+1} frames)")
    return tracks


def save_csv(tracks, csv_path, fps):
    """Write the tracking table to CSV: one row per (ball, frame).

    A `timestamp_ms` column is derived from `frame` and the video `fps`
    (timestamp_ms = frame / fps * 1000) because the downstream viscosity
    calculation works in real time, not frame index.

    Columns are ordered so the useful ones come first:
        particle     : the persistent track id (the number drawn on the video)
        frame        : frame index (0-based)
        timestamp_ms : frame time in milliseconds
        x, y         : sub-pixel centre position, in pixels
    followed by trackpy's feature columns (mass, size, ecc, signal, raw_mass,
    ep, ...). These are kept on purpose: downstream code selects the columns it
    needs *by name*, so the extras cost the consumer nothing and are handy for
    QC/debugging -- e.g. filter by `mass`, reject high-`ecc` blobs, or weight a
    fit by the localisation error `ep`. Rows are sorted by particle then frame
    so each ball's trajectory is contiguous and easy to read.
    """
    tracks = tracks.copy()
    tracks["timestamp_ms"] = tracks["frame"].to_numpy(dtype=float) / float(fps) * 1000.0
    front = ["particle", "frame", "timestamp_ms", "x", "y"]
    cols = [c for c in front if c in tracks.columns]
    cols += [c for c in tracks.columns if c not in cols]
    out = (tracks[cols]
           .sort_values(["particle", "frame"])
           .reset_index(drop=True))
    out.to_csv(csv_path, index=False)
    print(f"  wrote {len(out)} rows "
          f"({out['particle'].nunique()} balls across {out['frame'].nunique()} frames) "
          f"-> {csv_path}")


def track_video(video_path, csv_path=None, *,
                diameter=DEFAULTS["diameter"],
                minmass=DEFAULTS["minmass"],
                channel=DEFAULTS["channel"],
                invert=DEFAULTS["invert"],
                search_range=DEFAULTS["search_range"],
                memory=DEFAULTS["memory"],
                percentile=DEFAULTS["percentile"],
                workers=DEFAULTS["workers"],
                max_frames=0):
    """Track one video end-to-end and (optionally) write its CSV; no video output.

    This is the programmatic entry point used by the batch pipeline (main.py):
    load -> detect -> link -> save_csv. The annotated-video output lives only in
    the CLI (`main`), since it's just a visual sanity check while tuning.

    Returns
    -------
    (tracks, fps) : (pandas.DataFrame, float)
        The full tracking table (every particle) and the video frame rate.
    """
    frames, fps = load_frames(video_path)
    if max_frames:
        frames = frames[:max_frames]
    feats = detect(frames, channel, diameter, minmass, invert, percentile, workers)
    tracks = link(feats, search_range, memory)
    if csv_path is not None:
        save_csv(tracks, csv_path, fps)
    return tracks, fps


def _id_color(pid):
    """Deterministic bright BGR colour for a given particle id (stable across frames)."""
    rng = np.random.default_rng(int(pid) * 2654435761 % (2**32))
    hsv = np.uint8([[[rng.integers(0, 180), 200, 255]]])
    b, g, r = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(b), int(g), int(r)


def annotate_and_write(frames, tracks, out_path, fps, diameter, scale, draw_ids):
    """Draw circle + centre + id for every tracked ball and encode a video."""
    h, w = frames[0].shape[:2]
    radius = max(2, diameter // 2)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_path, fourcc, fps, (w * scale, h * scale))

    # group once for speed: frame index -> rows
    by_frame = {fr: g for fr, g in tracks.groupby("frame")}

    for i, bgr in enumerate(frames):
        canvas = cv2.resize(bgr, (w * scale, h * scale), interpolation=cv2.INTER_LINEAR)
        g = by_frame.get(i)
        if g is not None:
            for _, r in g.iterrows():
                x, y = int(round(r.x * scale)), int(round(r.y * scale))
                col = _id_color(r.particle)
                cv2.circle(canvas, (x, y), radius * scale, col, 1, cv2.LINE_AA)
                cv2.circle(canvas, (x, y), 1, (0, 255, 255), -1)  # yellow centre dot
                if draw_ids:
                    cv2.putText(canvas, str(int(r.particle)), (x + radius * scale, y - 2),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.3 * scale / 2,
                                col, 1, cv2.LINE_AA)
            label = f"frame {i}   balls {len(g)}"
        else:
            label = f"frame {i}   balls 0"
        cv2.putText(canvas, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (255, 255, 255), 2, cv2.LINE_AA)
        writer.write(canvas)
    writer.release()


def _percentile_arg(s):
    """argparse type: accept the string 'auto' or a numeric percentile."""
    return "auto" if s.lower() == "auto" else float(s)


def _workers_arg(s):
    """argparse type: accept the string 'auto' or an integer process count."""
    return "auto" if s.lower() == "auto" else int(s)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="input video path (e.g. rec1.mp4)")
    ap.add_argument("-o", "--output", default="tracked.mp4", help="output video path")
    ap.add_argument("--csv", default=None,
                    help="output CSV path for the tracking table "
                         "(default: same name as --output but with a .csv extension; "
                         "use --no-video to skip the video and only write the CSV)")
    ap.add_argument("--diameter", type=int, default=DEFAULTS["diameter"])
    ap.add_argument("--minmass", type=float, default=DEFAULTS["minmass"])
    ap.add_argument("--percentile", type=_percentile_arg, default=DEFAULTS["percentile"],
                    help="'auto' (default) sizes the detection percentile from frame-1 "
                         "bead occupancy; or pass a number 0-100 to force it")
    ap.add_argument("--workers", type=_workers_arg, default=DEFAULTS["workers"],
                    help="CPU processes for detection: 'auto' (cores-1), 1 (serial), "
                         "or an integer")
    ap.add_argument("--channel", type=int, default=DEFAULTS["channel"],
                    help="BGR channel for detection: 0=B 1=G 2=R")
    ap.add_argument("--invert", action="store_true",
                    help="set if balls are DARK on a bright background")
    ap.add_argument("--search-range", type=int, default=DEFAULTS["search_range"])
    ap.add_argument("--memory", type=int, default=DEFAULTS["memory"])
    ap.add_argument("--out-scale", type=int, default=DEFAULTS["out_scale"],
                    help="upscale factor for the output video (readability of ids)")
    ap.add_argument("--no-ids", action="store_true", help="do not draw id labels")
    ap.add_argument("--no-video", action="store_true",
                    help="skip the annotated video; write only the CSV")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="process only the first N frames (0 = all)")
    args = ap.parse_args()

    print(f"[1/5] loading {args.video} ...")
    frames, fps = load_frames(args.video)
    if args.max_frames:
        frames = frames[:args.max_frames]
    print(f"      {len(frames)} frames, {frames[0].shape[1]}x{frames[0].shape[0]}, {fps:.0f} fps")

    print("[2/5] detecting balls per frame ...")
    feats = detect(frames, args.channel, args.diameter, args.minmass,
                   args.invert or DEFAULTS["invert"], args.percentile, args.workers)

    print("[3/5] linking into tracks ...")
    tracks = link(feats, args.search_range, args.memory)

    csv_path = args.csv or os.path.splitext(args.output)[0] + ".csv"
    print(f"[4/5] writing tracking table -> {csv_path} ...")
    save_csv(tracks, csv_path, fps)

    if args.no_video:
        print("[5/5] skipping video (--no-video).")
    else:
        print(f"[5/5] drawing + writing {args.output} ...")
        annotate_and_write(frames, tracks, args.output, fps,
                           args.diameter, args.out_scale, not args.no_ids)
    print("done.")


if __name__ == "__main__":
    main()