"""
main.py - viscosity assembly line
=================================
Batch-process a folder of microscope videos into a single viscosity estimate.

Pipeline
--------
1. Track every video in  videos/   -> a per-video table in  CSVs/<name>.csv
   (uses track.track_video; no annotated video -- that is only for tuning).
2. For each CSV, keep only *well-tracked* beads: those present for at least
   MIN_COVERAGE of the recording. This drops beads the tracker jitters on and
   beads that drift in/out of frame partway through the clip.
3. Estimate viscosity per surviving bead (calculation.estimate_viscosity) using
   the KNOWN bead radius and the Stokes-Einstein relation.
4. Aggregate every bead from every video into one mean viscosity +/- uncertainty.

Edit the CONFIG block below to match your setup, then run:  python main.py
"""

import os
import numpy as np
import pandas as pd

from track import track_video, DEFAULTS
from calculation import estimate_viscosity


# =============================== CONFIG =======================================
VIDEO_DIR  = "videos"   # input videos live here
CSV_DIR    = "CSVs"     # per-video tracking tables are written here
VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv", ".m4v")

# Physics / calibration -- set these to YOUR experiment.
TEMPERATURE_K       = 298.15    # K
PIXEL_SIZE_M_PER_PX = 0.5e-6    # m per pixel (objective + camera calibration)
BEAD_RADIUS_M       = 0.5e-6    # known / monodisperse bead radius [m]
BEAD_RADIUS_UNC_M   = 0.0       # 1-sigma radius uncertainty [m] (polydispersity)

# Quality filter: keep a bead only if it is tracked for at least this fraction
# of the recording. Tune this; 0.90 = present in >= 90% of the video's frames.
MIN_COVERAGE = 0.90

# MSD fit / drift options (passed straight through to estimate_viscosity).
FIT_FRACTION     = 0.25
DRIFT_CORRECTION = True

# Re-run tracking even if a CSV already exists? False reuses CSVs/ (faster).
RETRACK = True

# Detection percentile. "auto" (the default) sizes it per video from how much of
# frame 1 the beads fill -- sparse clips run much faster, crowded clips keep every
# bead. Or set a number 0-100 to force one. See track.py choose_percentile().
PERCENTILE = DEFAULTS["percentile"]

# CPU processes for the detection step. "auto" = cores - 1 (leaves one core free so
# the machine stays responsive); 1 = serial; or an integer. Native threads are
# pinned to 1 per process so it can't oversubscribe the CPU. See track.py detect().
WORKERS = DEFAULTS["workers"]
# ==============================================================================


def track_all_videos(video_dir, csv_dir):
    """Track every video in `video_dir`, writing one CSV each into `csv_dir`."""
    if not os.path.isdir(video_dir):
        print(f"[track] no '{video_dir}/' folder found -- nothing to track.")
        return []
    os.makedirs(csv_dir, exist_ok=True)

    videos = sorted(f for f in os.listdir(video_dir)
                    if f.lower().endswith(VIDEO_EXTS))
    if not videos:
        print(f"[track] '{video_dir}/' has no videos ({', '.join(VIDEO_EXTS)}).")
        return []

    csv_paths = []
    for name in videos:
        video_path = os.path.join(video_dir, name)
        csv_path   = os.path.join(csv_dir, os.path.splitext(name)[0] + ".csv")
        if os.path.exists(csv_path) and not RETRACK:
            print(f"[track] {name}: CSV exists, skipping (RETRACK=False).")
        else:
            print(f"[track] {name} -> {csv_path}")
            track_video(
                video_path, csv_path,
                diameter=DEFAULTS["diameter"], minmass=DEFAULTS["minmass"],
                channel=DEFAULTS["channel"], invert=DEFAULTS["invert"],
                search_range=DEFAULTS["search_range"], memory=DEFAULTS["memory"],
                percentile=PERCENTILE, workers=WORKERS,
            )
        csv_paths.append(csv_path)
    return csv_paths


def well_tracked_particles(df, min_coverage):
    """Particle ids present for >= `min_coverage` of the recording's frames."""
    n_total = int(df["frame"].max() - df["frame"].min() + 1)
    if n_total <= 0:
        return []
    frames_per_particle = df.groupby("particle")["frame"].nunique()
    coverage = frames_per_particle / n_total
    return coverage[coverage >= min_coverage].index.tolist()


def viscosities_from_csv(csv_path):
    """Estimate viscosity for every well-tracked bead in one CSV."""
    df = pd.read_csv(csv_path)
    keep = well_tracked_particles(df, MIN_COVERAGE)
    print(f"[calc]  {os.path.basename(csv_path)}: "
          f"{df['particle'].nunique()} beads, "
          f"{len(keep)} pass >= {MIN_COVERAGE:.0%} coverage")

    results = []
    for pid in keep:
        sub = df[df["particle"] == pid]
        try:
            res = estimate_viscosity(
                sub,
                temperature_K       = TEMPERATURE_K,
                pixel_size_m_per_px = PIXEL_SIZE_M_PER_PX,
                particle_radius_m   = BEAD_RADIUS_M,
                particle_radius_uncertainty_m = BEAD_RADIUS_UNC_M,
                fit_fraction        = FIT_FRACTION,
                drift_correction    = DRIFT_CORRECTION,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"          particle {pid}: skipped ({exc})")
            continue
        res["source"]   = os.path.basename(csv_path)
        res["particle"] = int(pid)
        results.append(res)
    return results


def summarise(results):
    """Combine per-bead viscosities into a mean +/- uncertainty."""
    eta = np.array([r["viscosity_Pa_s"] for r in results], dtype=float)
    sig = np.array([r["viscosity_uncertainty_Pa_s"] for r in results], dtype=float)
    n = len(eta)

    summary = {
        "n_particles": n,
        "mean_Pa_s":   float(eta.mean()),
        "std_Pa_s":    float(eta.std(ddof=1)) if n > 1 else float("nan"),
        "sem_Pa_s":    (float(eta.std(ddof=1) / np.sqrt(n)) if n > 1
                        else (float(sig[0]) if n == 1 else float("nan"))),
    }

    # Inverse-variance weighted mean (each bead weighted by 1 / sigma^2).
    good = np.isfinite(sig) & (sig > 0) & np.isfinite(eta)
    if good.any():
        w = 1.0 / sig[good] ** 2
        summary["weighted_mean_Pa_s"] = float(np.sum(w * eta[good]) / np.sum(w))
        summary["weighted_unc_Pa_s"]  = float(1.0 / np.sqrt(np.sum(w)))
    else:
        summary["weighted_mean_Pa_s"] = float("nan")
        summary["weighted_unc_Pa_s"]  = float("nan")
    return summary


def main():
    print("=" * 70)
    print("STEP 1/2 - tracking videos")
    print("=" * 70)
    csv_paths = track_all_videos(VIDEO_DIR, CSV_DIR)

    if not csv_paths:
        # Fall back to any CSVs already sitting in CSV_DIR.
        if os.path.isdir(CSV_DIR):
            csv_paths = [os.path.join(CSV_DIR, f) for f in sorted(os.listdir(CSV_DIR))
                         if f.lower().endswith(".csv")]
        if not csv_paths:
            print(f"\nNothing to process. Add videos to '{VIDEO_DIR}/' "
                  f"or CSVs to '{CSV_DIR}/'.")
            return
        print(f"\nUsing {len(csv_paths)} existing CSV(s) in '{CSV_DIR}/'.")

    print("\n" + "=" * 70)
    print("STEP 2/2 - estimating viscosity per bead")
    print("=" * 70)
    results = []
    for csv_path in csv_paths:
        results.extend(viscosities_from_csv(csv_path))

    if not results:
        print("\nNo beads passed the filter / fit. "
              "Try lowering MIN_COVERAGE or revisiting the tracking parameters.")
        return

    # Per-bead table.
    print("\nPer-bead estimates:")
    print(f"  {'source':<22} {'id':>4} {'eta[Pa.s]':>12} {'+/-[Pa.s]':>12} "
          f"{'D[m2/s]':>12} {'R2':>6} {'pts':>5}")
    for r in results:
        print(f"  {r['source']:<22} {r['particle']:>4} "
              f"{r['viscosity_Pa_s']:>12.4e} "
              f"{r['viscosity_uncertainty_Pa_s']:>12.4e} "
              f"{r['diffusion_m2_s']:>12.4e} {r['fit_r2']:>6.3f} "
              f"{r['num_points']:>5}")

    # Aggregate.
    s = summarise(results)
    print("\n" + "-" * 70)
    print(f"Beads used                : {s['n_particles']}")
    print(f"Mean viscosity            : {s['mean_Pa_s']:.4e} Pa.s")
    print(f"  std across beads        : {s['std_Pa_s']:.4e} Pa.s")
    print(f"  SEM of the mean         : {s['sem_Pa_s']:.4e} Pa.s")
    print(f"Inverse-variance weighted : {s['weighted_mean_Pa_s']:.4e} "
          f"+/- {s['weighted_unc_Pa_s']:.4e} Pa.s")
    print("-" * 70)


if __name__ == "__main__":
    main()
