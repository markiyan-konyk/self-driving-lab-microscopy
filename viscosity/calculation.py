"""
estimate_viscosity.py
─────────────────────
Estimate viscosity from 2-D single-particle tracking via
the Stokes–Einstein relation.

Theory
------
For a spherical particle undergoing free Brownian motion in 2D:

    MSD(τ) = ⟨|r(t+τ) − r(t)|²⟩ = 4 D τ          (2-D)

Stokes drag on a sphere of radius r in a 3-D fluid:

    γ = 6π η r

Stokes–Einstein:

    D = k_B T / (6π η r)
    ⟹  η = k_B T / (6π r D)

Usage
-----
    from estimate_viscosity import estimate_viscosity
    result = estimate_viscosity(
        csv_file        = "track.csv",
        temperature_K   = 298.15,
        pixel_size_m_per_px = 0.5e-6,   # e.g. 0.5 µm / px
    )
    print(result)
"""

import warnings
import numpy as np
import pandas as pd
from scipy.stats import linregress          # kept as optional reference

KB = 1.380649e-23  # J / K


# ──────────────────────────────────────────────────────────────────────────────
def estimate_viscosity(
    csv_file,
    temperature_K: float,
    pixel_size_m_per_px: float,
    particle_radius_m: float,
    particle_radius_uncertainty_m: float = 0.0,
    fit_fraction: float = 0.25,
    drift_correction: bool = True,
) -> dict:
    """
    Estimate dynamic viscosity from one particle's 2-D tracking data.

    Parameters
    ----------
    csv_file : str | path | file-like | pandas.DataFrame
        Single-particle trajectory with columns: timestamp_ms, x, y.
        A `particle` column is allowed but must contain only one id — this
        routine models a single trajectory, so pre-filter multi-particle tables.
    temperature_K : float
        Absolute temperature [K]
    pixel_size_m_per_px : float
        Physical size of one pixel [m/px]
    particle_radius_m : float
        Known particle radius [m] (e.g. calibrated / monodisperse beads). Used
        directly in Stokes–Einstein rather than measured from the image, which
        is far more accurate for sub-micron beads where the imaged blob size is
        dominated by the optics.
    particle_radius_uncertainty_m : float
        1σ uncertainty on the radius [m] (e.g. from bead polydispersity).
        Defaults to 0 (treat the radius as exact).
    fit_fraction : float
        Fraction of MSD lag range used for the linear fit (0 < fit_fraction ≤ 1).
        MSD is reliable only at short lags (typically ≤ N/4), so values above
        0.5 are rarely useful.
    drift_correction : bool
        Remove a linear (constant-velocity) stage drift before computing MSD.

    Returns
    -------
    dict with keys:
        viscosity_Pa_s, viscosity_uncertainty_Pa_s,
        diffusion_m2_s, diffusion_uncertainty_m2_s,
        particle_radius_m, particle_radius_uncertainty_m,
        fit_r2, slope_m2_s, slope_uncertainty_m2_s,
        intercept_m2, intercept_uncertainty_m2,
        num_points, fit_points,
        intercept_warning (str | None)
    """
    # ── 1. Load & validate ────────────────────────────────────────────────────
    df = csv_file.copy() if isinstance(csv_file, pd.DataFrame) else pd.read_csv(csv_file)

    # This routine models ONE particle. A multi-particle table (sorted only by
    # time) would interleave different beads, and the MSD below would difference
    # positions of *different* particles → silent garbage. Fail loudly instead.
    if "particle" in df.columns and df["particle"].nunique() > 1:
        raise ValueError(
            f"estimate_viscosity expects a single particle's trajectory, but got "
            f"{df['particle'].nunique()} particles — filter by `particle` first."
        )

    required = {"timestamp_ms", "x", "y"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    df = (
        df[list(required)]
        .dropna()
        .sort_values("timestamp_ms")
        .reset_index(drop=True)
    )

    if len(df) < 20:
        raise ValueError("Trajectory too short (< 20 points).")

    n = len(df)

    # ── 2. Unit conversion ───────────────────────────────────────────────────
    # Time in seconds, zero-referenced
    t = (df["timestamp_ms"].to_numpy(dtype=float) - df["timestamp_ms"].iloc[0]) / 1e3

    # Positions in metres
    x = df["x"].to_numpy(dtype=float) * pixel_size_m_per_px
    y = df["y"].to_numpy(dtype=float) * pixel_size_m_per_px

    # Radius: known value supplied by the caller (not measured from the image).
    radius_m     = float(particle_radius_m)
    radius_unc_m = float(particle_radius_uncertainty_m)
    if radius_m <= 0:
        raise ValueError("particle_radius_m must be positive.")

    # ── 3. Drift correction (linear) ─────────────────────────────────────────
    if drift_correction:
        px = np.polyfit(t, x, 1)
        py = np.polyfit(t, y, 1)
        x = x - np.polyval(px, t)
        y = y - np.polyval(py, t)

    # ── 4. MSD via ensemble average over all lag-τ pairs ─────────────────────
    # FIX: max_lag is now derived from fit_fraction so that we always
    # compute at least as many lag points as the fit will use.
    # The reliability limit n//4 is still respected as an upper bound.
    reliability_limit = n // 4
    max_lag = min(reliability_limit, max(5, int(n * fit_fraction)))

    tau     = np.empty(max_lag)
    msd     = np.empty(max_lag)
    msd_sem = np.empty(max_lag)

    for lag in range(1, max_lag + 1):
        dx = x[lag:] - x[:-lag]
        dy = y[lag:] - y[:-lag]
        disp2 = dx**2 + dy**2

        # Mean lag time (exact even for irregular timestamps)
        tau[lag - 1]     = np.mean(t[lag:] - t[:-lag])
        msd[lag - 1]     = np.mean(disp2)
        # FIX: guard against n_pairs == 1 (ddof=1 would give NaN)
        n_pairs = len(disp2)
        msd_sem[lag - 1] = (
            np.std(disp2, ddof=1) / np.sqrt(n_pairs)
            if n_pairs > 1 else np.nan
        )

    # ── 5. Weighted linear fit: MSD = slope·τ + intercept ───────────────────
    n_fit    = max(5, int(max_lag * fit_fraction))
    tau_fit  = tau[:n_fit]
    msd_fit  = msd[:n_fit]
    sem_fit  = msd_sem[:n_fit]

    # Replace NaN / zero SEM with the median so the fit doesn't blow up.
    # This happens at very short trajectories where some lag has only 1 pair.
    valid_sem = sem_fit[np.isfinite(sem_fit) & (sem_fit > 0)]
    if len(valid_sem) == 0:
        raise RuntimeError("All MSD SEM values are NaN/zero - trajectory too short.")
    fallback_sem = np.median(valid_sem)
    sem_fit = np.where(np.isfinite(sem_fit) & (sem_fit > 0), sem_fit, fallback_sem)

    # WLS weights: w_i = 1 / σ_i
    # np.polyfit minimises Σ [w_i · (y_i − ŷ_i)]²,
    # so passing w = 1/σ is equivalent to standard WLS with var-weights 1/σ².
    w = 1.0 / sem_fit  # shape (n_fit,)

    # FIX: covariance from np.polyfit is scaled by the residual variance
    # (i.e. it is the "raw" parameter covariance, not the weighted one).
    # We extract σ_slope directly from the diagonal of the returned cov matrix;
    # this is correct when the weights accurately reflect the true σ_i.
    coeffs, cov = np.polyfit(tau_fit, msd_fit, deg=1, w=w, cov=True)
    slope, intercept = coeffs
    slope_se     = np.sqrt(cov[0, 0])
    intercept_se = np.sqrt(cov[1, 1])

    if slope <= 0:
        raise RuntimeError(
            f"Fitted slope is non-positive ({slope:.3e} m^2/s). "
            "Check for insufficient displacement or over-correction of drift."
        )

    # ── 6. Diffusion coefficient: MSD = 4D·τ  (2-D) ─────────────────────────
    D    = slope / 4.0
    D_se = slope_se / 4.0

    # ── 7. Viscosity via Stokes–Einstein ─────────────────────────────────────
    viscosity = KB * temperature_K / (6.0 * np.pi * radius_m * D)

    # Uncertainty propagation (uncorrelated r and D):
    #   σ_η/η = sqrt( (σ_r/r)² + (σ_D/D)² )
    rel_var         = (radius_unc_m / radius_m) ** 2 + (D_se / D) ** 2
    viscosity_se    = viscosity * np.sqrt(rel_var)

    # ── 8. Goodness of fit ───────────────────────────────────────────────────
    predicted = slope * tau_fit + intercept
    ss_res    = np.sum((msd_fit - predicted) ** 2)
    ss_tot    = np.sum((msd_fit - msd_fit.mean()) ** 2)
    r2        = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan

    # FIX: intercept sanity check (should be ~0 for pure Brownian motion)
    intercept_warning = None
    intercept_threshold = 2.0 * intercept_se  # 2-sigma check
    if abs(intercept) > intercept_threshold:
        if intercept > 0:
            intercept_warning = (
                f"Positive intercept ({intercept:.3e} m^2) exceeds 2 sigma. "
                "This may indicate localisation noise (add noise floor to model)."
            )
        else:
            intercept_warning = (
                f"Negative intercept ({intercept:.3e} m^2) exceeds 2 sigma. "
                "Possible over-correction of drift."
            )
        warnings.warn(intercept_warning)

    return {
        "viscosity_Pa_s":               viscosity,
        "viscosity_uncertainty_Pa_s":   viscosity_se,
        "diffusion_m2_s":               D,
        "diffusion_uncertainty_m2_s":   D_se,
        "particle_radius_m":            radius_m,
        "particle_radius_uncertainty_m": radius_unc_m,
        "fit_r2":                       r2,
        "slope_m2_s":                   slope,
        "slope_uncertainty_m2_s":       slope_se,
        "intercept_m2":                 intercept,
        "intercept_uncertainty_m2":     intercept_se,
        "num_points":                   n,
        "fit_points":                   n_fit,
        "intercept_warning":            intercept_warning,
    }


# ── Quick smoke-test (runs when executed directly) ────────────────────────────
if __name__ == "__main__":
    import io, textwrap

    # Synthetic Brownian walk for testing
    rng   = np.random.default_rng(42)
    n_pts = 500
    dt    = 0.1        # s
    D_true   = 1e-13   # m²/s
    r_true   = 1e-6    # 1 µm
    T        = 298.15  # K
    px_size  = 0.5e-6  # m/px  →  1 px = 0.5 µm

    eta_true = KB * T / (6 * np.pi * r_true * D_true)
    print(f"True eta = {eta_true:.4f} Pa.s")

    steps  = rng.normal(0, np.sqrt(2 * D_true * dt), (n_pts, 2))
    pos    = np.cumsum(steps, axis=0)          # metres
    pos_px = pos / px_size                     # pixels
    ts_ms  = np.arange(n_pts) * dt * 1000 + 1_718_000_000_000

    fake_csv = io.StringIO()
    df_sim = pd.DataFrame({
        "timestamp_ms": ts_ms.astype(int),
        "x":            pos_px[:, 0],
        "y":            pos_px[:, 1],
    })
    df_sim.to_csv(fake_csv, index=False)
    fake_csv.seek(0)

    res = estimate_viscosity(
        fake_csv,
        temperature_K       = T,
        pixel_size_m_per_px = px_size,
        particle_radius_m   = r_true,
        fit_fraction        = 0.25,
        drift_correction    = True,
    )

    print("\nEstimated results:")
    for k, v in res.items():
        if v is None:
            print(f"  {k}: None")
        elif isinstance(v, float):
            print(f"  {k}: {v:.4e}")
        else:
            print(f"  {k}: {v}")

