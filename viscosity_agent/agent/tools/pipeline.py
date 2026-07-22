"""The analysis pipeline: track -> QC -> estimate -> aggregate.

Thin wrappers over the repo's validated viscosity/ code (detect, link,
estimate_viscosity, well_tracked_particles, summarise). The one thing we do
differently from the offline pipeline is timestamps: we merge the REAL
per-frame arrival times captured during recording into the tracking table, so
estimate_viscosity works in true time instead of a fabricated frame/fps clock.
"""

import glob
import os

import numpy as np
import pandas as pd

from .. import _viscosity_import as _vi
from ..context import Context

_D = _vi.DEFAULTS


def _abs(ctx, rel):
    return rel if os.path.isabs(rel) else os.path.join(ctx.run_dir, rel)


def _load_frames(frames_dir):
    """Decode every JPEG in a clip's frames/ dir into a BGR list, in order."""
    import cv2
    paths = sorted(glob.glob(os.path.join(frames_dir, "frame_*.jpg")))
    frames = []
    for p in paths:
        img = cv2.imread(p, cv2.IMREAD_COLOR)
        if img is not None:
            frames.append(img)
    return frames


def _workers(ctx):
    w = str(ctx.cfg.track_workers)
    return "auto" if w.lower() == "auto" else int(w)


def track_params(ctx: Context) -> dict:
    """Resolve the trackpy detection/linking params: viscosity DEFAULTS, overlaid
    with anything the agent has re-tuned onto ``ctx.track_params``."""
    p = dict(_D)
    override = getattr(ctx, "track_params", None) or {}
    p.update({k: v for k, v in override.items() if v is not None})
    return p


def track_clip(ctx: Context, clip_record: dict):
    """Detect + link a recorded clip, writing a real-timestamp CSV. Updates the record."""
    clip_id = clip_record["clip_id"]
    clip_dir = _abs(ctx, clip_record["dir"])
    frames_dir = os.path.join(clip_dir, "frames")

    frames = _load_frames(frames_dir)
    if len(frames) < 20:
        ctx.nb.error(f"track_clip {clip_id}: only {len(frames)} frames, skipping")
        clip_record["csv_path"] = None
        clip_record["n_beads_total"] = 0
        return clip_record

    p = track_params(ctx)
    ctx.nb.phase(f"tracking {clip_id} ({len(frames)} frames, "
                 f"diameter={p['diameter']} minmass={p['minmass']} "
                 f"percentile={p['percentile']})", clip_id=clip_id)
    feats = _vi.detect(frames, p["channel"], p["diameter"], p["minmass"],
                       p["invert"], percentile=p["percentile"], workers=_workers(ctx))
    tracks = _vi.link(feats, p["search_range"], p["memory"])
    del frames                      # free the decoded frames promptly

    # merge REAL timestamps (frame -> timestamp_ms) captured during recording
    ts_path = os.path.join(clip_dir, "timestamps.csv")
    ts = pd.read_csv(ts_path)
    tracks = tracks.merge(ts, on="frame", how="left")
    if tracks["timestamp_ms"].isna().any():
        # any frame without a real stamp (shouldn't happen) falls back to nominal
        fps = clip_record.get("measured_fps") or ctx.cfg.target_fps
        nan = tracks["timestamp_ms"].isna()
        tracks.loc[nan, "timestamp_ms"] = tracks.loc[nan, "frame"] / fps * 1000.0

    front = ["particle", "frame", "timestamp_ms", "x", "y"]
    cols = [c for c in front if c in tracks.columns]
    cols += [c for c in tracks.columns if c not in cols]
    out = tracks[cols].sort_values(["particle", "frame"]).reset_index(drop=True)

    csv_dir = os.path.join(ctx.run_dir, "csvs")
    os.makedirs(csv_dir, exist_ok=True)
    csv_path = os.path.join(csv_dir, f"{clip_id}.csv")
    out.to_csv(csv_path, index=False)

    n_beads = int(out["particle"].nunique())
    med_len = int(out.groupby("particle").size().median())
    clip_record["csv_path"] = os.path.relpath(csv_path, ctx.run_dir).replace(os.sep, "/")
    clip_record["n_beads_total"] = n_beads
    clip_record["median_track_len"] = med_len
    ctx.nb.result(f"{clip_id}: {n_beads} beads linked (median track {med_len} frames)",
                  clip_id=clip_id, n_beads=n_beads, median_track_len=med_len)
    return clip_record


def qc_tracks(ctx: Context, clip_record: dict):
    """Keep only well-tracked, non-elongated beads. Returns a DatasetRecord dict."""
    clip_id = clip_record["clip_id"]
    csv_path = clip_record.get("csv_path")
    if not csv_path:
        return {"clip_id": clip_id, "n_beads_total": 0, "n_beads_kept": 0,
                "kept_ids": [], "min_coverage": ctx.cfg.min_coverage,
                "max_ecc": ctx.cfg.max_ecc}
    df = pd.read_csv(_abs(ctx, csv_path))
    kept = _vi.well_tracked_particles(df, ctx.cfg.min_coverage)

    # reject elongated blobs (clumps / debris) by median eccentricity
    good = []
    for pid in kept:
        sub = df[df["particle"] == pid]
        if "ecc" in sub.columns and np.median(sub["ecc"].to_numpy()) > ctx.cfg.max_ecc:
            continue
        good.append(int(pid))

    ds = {"clip_id": clip_id, "n_beads_total": int(df["particle"].nunique()),
          "n_beads_kept": len(good), "kept_ids": good,
          "min_coverage": ctx.cfg.min_coverage, "max_ecc": ctx.cfg.max_ecc}
    ctx.nb.result(
        f"{clip_id} QC: {ds['n_beads_kept']}/{ds['n_beads_total']} beads pass "
        f">= {ctx.cfg.min_coverage:.0%} coverage & ecc <= {ctx.cfg.max_ecc}",
        clip_id=clip_id, n_kept=ds["n_beads_kept"], n_total=ds["n_beads_total"])
    return ds


def estimate_all(ctx: Context, clip_record: dict, dataset: dict, um_per_px: float):
    """Estimate viscosity per surviving bead. Returns a list of BeadResult dicts."""
    clip_id = clip_record["clip_id"]
    csv_path = clip_record.get("csv_path")
    if not csv_path or not dataset.get("kept_ids"):
        return []
    df = pd.read_csv(_abs(ctx, csv_path))
    px_size_m = float(um_per_px) * 1e-6

    results = []
    skipped = 0
    for pid in dataset["kept_ids"]:
        sub = df[df["particle"] == pid]
        try:
            res = _vi.estimate_viscosity(
                sub,
                temperature_K=ctx.cfg.temperature_K,
                pixel_size_m_per_px=px_size_m,
                particle_radius_m=ctx.cfg.bead_radius_m,
                particle_radius_uncertainty_m=ctx.cfg.bead_radius_unc_m,
                fit_fraction=ctx.cfg.fit_fraction,
                drift_correction=ctx.cfg.drift_correction,
            )
        except (ValueError, RuntimeError):
            skipped += 1
            continue
        results.append({
            "clip_id": clip_id, "particle": int(pid),
            "viscosity_Pa_s": float(res["viscosity_Pa_s"]),
            "viscosity_uncertainty_Pa_s": float(res["viscosity_uncertainty_Pa_s"]),
            "diffusion_m2_s": float(res["diffusion_m2_s"]),
            "fit_r2": float(res["fit_r2"]),
            "num_points": int(res["num_points"]),
            "intercept_warning": res["intercept_warning"],
        })
    ctx.nb.result(
        f"{clip_id}: estimated {len(results)} beads"
        + (f" ({skipped} skipped)" if skipped else ""),
        clip_id=clip_id, n_estimated=len(results), n_skipped=skipped)
    return results


def aggregate_results(results: list):
    """Inverse-variance weighted aggregate over every bead (viscosity/main.summarise)."""
    if not results:
        return None
    return _vi.summarise(results)


def reanalyze_all(ctx: Context, clips: list, um_per_px: float):
    """Re-run QC + estimate across every already-tracked clip with the CURRENT
    analysis params (ctx.cfg). Returns (datasets, results, aggregate).

    Used by the critique agent's ``reanalyze`` tool to commit a methodology change
    (e.g. toggled drift correction, looser coverage) to the whole dataset without
    re-recording anything.
    """
    datasets, results = [], []
    for clip in clips:
        if not clip.get("csv_path"):
            continue
        ds = qc_tracks(ctx, clip)
        datasets.append(ds)
        results.extend(estimate_all(ctx, clip, ds, um_per_px))
    return datasets, results, aggregate_results(results)
