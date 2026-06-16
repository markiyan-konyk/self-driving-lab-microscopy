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

The CSV columns are: particle, frame, x, y, mass, size, ecc, signal, raw_mass, ep.
`particle` is the id drawn on the video; (x, y) are sub-pixel positions in pixels.

The defaults below are tuned for the supplied 640x480 / 60 fps clip.
"""

import argparse
import os
import time
import cv2
import numpy as np
import pandas as pd
import trackpy as tp


# --------------------------------------------------------------------------- #
#  Defaults (tuned for the 640x480 clip -- see the README notes at the bottom) #
# --------------------------------------------------------------------------- #
DEFAULTS = dict(
    diameter=11,      # apparent feature size in px; MUST be odd. Bigger video => bigger value.
    minmass=800,      # min integrated brightness to keep a feature (rejects noise).
    channel=1,        # OpenCV BGR channel used for detection: 0=B, 1=G, 2=R. Green wins here.
    invert=False,     # balls are BRIGHT on a darker background -> False. Flip if yours are dark.
    search_range=5,   # max px a ball may move between frames (measured ~0.8px max here).
    memory=3,         # frames a ball may vanish (e.g. behind a blur) and still keep its id.
    out_scale=2,      # upscale the OUTPUT video so the tiny id labels are readable.
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


def detect(frames, channel, diameter, minmass, invert):
    """Run trackpy.locate on every frame. Returns one concatenated DataFrame.

    This is equivalent to trackpy.batch(), but written as an explicit loop so we
    can print progress / an ETA, which matters on longer clips.
    """
    tp.quiet()  # silence trackpy's own per-frame logging
    all_feats = []
    t0 = time.time()
    n = len(frames)
    for i, bgr in enumerate(frames):
        plane = bgr[:, :, channel]
        f = tp.locate(plane, diameter=diameter, minmass=minmass, invert=invert)
        f["frame"] = i
        all_feats.append(f)
        if (i + 1) % 25 == 0 or i == n - 1:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (n - i - 1) / rate
            print(f"  detect {i+1:4d}/{n}  "
                  f"({rate:5.1f} frame/s, ETA {eta:4.1f}s, "
                  f"{len(f)} balls this frame)")
    return pd.concat(all_feats, ignore_index=True)


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


def save_csv(tracks, csv_path):
    """Write the tracking table to CSV: one row per (ball, frame).

    Columns are ordered so the useful ones come first:
        particle : the persistent track id (the number drawn on the video)
        frame    : frame index (0-based)
        x, y     : sub-pixel centre position, in pixels
    followed by trackpy's feature columns (mass, size, ecc, signal, raw_mass,
    ep, ...) which you'll want downstream -- e.g. filtering by `mass`, or
    feeding x/y/frame/particle into tp.imsd / tp.emsd for diffusion analysis.
    Rows are sorted by particle then frame so each ball's trajectory is
    contiguous and easy to read.
    """
    front = ["particle", "frame", "x", "y"]
    cols = [c for c in front if c in tracks.columns]
    cols += [c for c in tracks.columns if c not in cols]
    out = (tracks[cols]
           .sort_values(["particle", "frame"])
           .reset_index(drop=True))
    out.to_csv(csv_path, index=False)
    print(f"  wrote {len(out)} rows "
          f"({out['particle'].nunique()} balls across {out['frame'].nunique()} frames) "
          f"-> {csv_path}")


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
                   args.invert or DEFAULTS["invert"])

    print("[3/5] linking into tracks ...")
    tracks = link(feats, args.search_range, args.memory)

    csv_path = args.csv or os.path.splitext(args.output)[0] + ".csv"
    print(f"[4/5] writing tracking table -> {csv_path} ...")
    save_csv(tracks, csv_path)

    if args.no_video:
        print("[5/5] skipping video (--no-video).")
    else:
        print(f"[5/5] drawing + writing {args.output} ...")
        annotate_and_write(frames, tracks, args.output, fps,
                           args.diameter, args.out_scale, not args.no_ids)
    print("done.")


if __name__ == "__main__":
    main()