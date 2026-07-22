"""The critique agent's sandbox: write/read/list files and run Python, all
jailed to ``run_dir/workspace/``. This is what lets the agent write and run its
OWN analysis scripts (MSD-linearity plots, fit-fraction sensitivity, outlier
checks, histograms) instead of only trusting the numbers handed to it.

Safety: paths are resolved and confined to the workspace; run_python spawns a
fresh interpreter with API keys / secrets stripped from the environment,
MPLBACKEND forced to Agg (headless plotting), output truncated, and a hard
timeout. The interpreter can still read/write inside the workspace, where we've
pre-staged the run's tracking CSVs and result JSON under ./data/.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

from ..context import Context
from . import pipeline

_MAX_OUTPUT = 6000            # chars of combined stdout+stderr returned to the LLM
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def _workspace(ctx: Context) -> str:
    ws = os.path.join(ctx.run_dir, "workspace")
    os.makedirs(os.path.join(ws, "data"), exist_ok=True)
    os.makedirs(os.path.join(ws, "plots"), exist_ok=True)
    return ws


def prepare_workspace(ctx: Context, state: dict) -> str:
    """Stage the run's data under workspace/data/ so sandbox scripts can load it."""
    ws = _workspace(ctx)
    data = os.path.join(ws, "data")
    for clip in state.get("clips", []):
        csv_rel = clip.get("csv_path")
        if not csv_rel:
            continue
        src = os.path.join(ctx.run_dir, csv_rel)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(data, f"{clip['clip_id']}.csv"))
    with open(os.path.join(data, "results.json"), "w", encoding="utf-8") as f:
        json.dump(state.get("results", []), f, indent=2)
    with open(os.path.join(data, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({
            "aggregate": state.get("aggregate"),
            "temperature_K": ctx.cfg.temperature_K,
            "bead_radius_m": ctx.cfg.bead_radius_m,
            "um_per_px": state.get("um_per_px"),
            "pixel_size_m_per_px": (state.get("um_per_px") or 0) * 1e-6,
            "fit_fraction": ctx.cfg.fit_fraction,
            "literature_eta_Pa_s": ctx.cfg.literature_eta_Pa_s,
        }, f, indent=2)
    return ws


def _safe_path(ws: str, filename: str) -> Path:
    p = (Path(ws) / filename).resolve()
    if not p.is_relative_to(Path(ws).resolve()):
        raise ValueError(f"path escapes the workspace: {filename!r}")
    return p


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if not any(h in k.upper() for h in _SECRET_HINTS)}
    env["MPLBACKEND"] = "Agg"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def build_analysis_tools(ctx: Context, state: dict):
    """Return the LangChain tools the critique agent may call (closed over ctx)."""
    ws = prepare_workspace(ctx, state)
    nb = ctx.nb

    @tool
    def write_file(filename: str, content: str) -> str:
        """Create or overwrite a file in the workspace (e.g. an analysis script
        analyze.py). Use relative paths like 'analyze.py' or 'plots/msd.png'."""
        p = _safe_path(ws, filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        nb.tool("write_file", {"filename": filename}, f"{len(content)} bytes")
        return f"wrote {filename} ({len(content)} bytes)"

    @tool
    def read_file(filename: str) -> str:
        """Read a text file from the workspace (your scripts, their output, or a
        staged data file under data/)."""
        p = _safe_path(ws, filename)
        if not p.is_file():
            return f"no such file: {filename}"
        text = p.read_text(encoding="utf-8", errors="replace")
        nb.tool("read_file", {"filename": filename}, f"{len(text)} bytes")
        return text[:_MAX_OUTPUT]

    @tool
    def list_files() -> str:
        """List all files currently in the workspace (recursively)."""
        out = []
        for root, _dirs, files in os.walk(ws):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), ws).replace(os.sep, "/")
                out.append(rel)
        nb.tool("list_files", {}, f"{len(out)} files")
        return "\n".join(sorted(out)) or "(empty)"

    @tool
    def run_python(filename: str, timeout_s: int = 60) -> str:
        """Run a Python script that already exists in the workspace and return its
        combined stdout+stderr. numpy/pandas/scipy/matplotlib are available; the
        run's tracking CSVs and results are under ./data/ (see data/summary.json).
        Plots must be saved to files (headless) -- e.g. plt.savefig('plots/x.png')."""
        timeout_s = int(max(1, min(ctx.cfg.sandbox_timeout_s, timeout_s)))
        p = _safe_path(ws, filename)
        if not p.is_file():
            return f"no such file: {filename} (write it first with write_file)"
        try:
            proc = subprocess.run(
                [sys.executable, str(p)], cwd=ws, env=_clean_env(),
                capture_output=True, text=True, timeout=timeout_s)
            out = (proc.stdout or "") + (
                ("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
            status = f"exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            out, status = "", f"TIMEOUT after {timeout_s}s"
        nb.tool("run_python", {"filename": filename, "timeout_s": timeout_s},
                status)
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + "\n…[truncated]"
        return f"[{status}]\n{out}"

    @tool
    def read_results_summary() -> str:
        """Return the current aggregate viscosity, the per-bead results table, and
        the experiment constants (temperature, bead radius, calibration, literature
        value) as JSON."""
        agg = state.get("aggregate")
        results = state.get("results", [])
        summary = {
            "aggregate": agg,
            "n_beads": len(results),
            "per_bead": [
                {k: r[k] for k in ("clip_id", "particle", "viscosity_Pa_s",
                                   "viscosity_uncertainty_Pa_s", "diffusion_m2_s",
                                   "fit_r2", "num_points", "intercept_warning")}
                for r in results],
            "temperature_K": ctx.cfg.temperature_K,
            "bead_radius_m": ctx.cfg.bead_radius_m,
            "um_per_px": state.get("um_per_px"),
            "literature_eta_Pa_s": ctx.cfg.literature_eta_Pa_s,
        }
        nb.tool("read_results_summary", {}, f"{len(results)} beads")
        return json.dumps(summary, indent=2, default=str)

    @tool
    def read_tracks_head(clip_id: str, n: int = 10) -> str:
        """Return the first n rows of a clip's tracking CSV (columns: particle,
        frame, timestamp_ms, x, y, mass, size, ecc, ...)."""
        import pandas as pd
        path = os.path.join(ws, "data", f"{clip_id}.csv")
        if not os.path.isfile(path):
            return f"no data for clip {clip_id}"
        head = pd.read_csv(path, nrows=int(max(1, min(200, n)))).to_csv(index=False)
        nb.tool("read_tracks_head", {"clip_id": clip_id, "n": n}, "ok")
        return head

    # ---- closed-loop re-tuning: change the method and COMMIT it to the data ----
    def _eta(agg):
        if not agg or agg.get("weighted_mean_Pa_s") is None:
            return "n/a"
        return (f"{agg['weighted_mean_Pa_s']:.3e} Pa.s (N={agg['n_particles']}, "
                f"+/-{agg['weighted_unc_Pa_s']:.1e})")

    @tool
    def set_tracking_params(diameter: int = None, minmass: float = None,
                            percentile: float = None, search_range: int = None,
                            memory: int = None) -> str:
        """Re-tune the trackpy DETECTION/LINKING parameters for future re-processing.
        Use when you suspect beads are being missed (lower minmass, adjust diameter)
        or mislinked (raise search_range/memory). diameter must be odd. After setting,
        call reprocess_clip(clip_id) to actually re-detect, then reanalyze(). Only the
        params you pass change; omit the rest."""
        tp = ctx.track_params
        if diameter is not None:
            d = int(diameter)
            tp["diameter"] = d if d % 2 == 1 else d + 1      # trackpy needs odd
        if minmass is not None:
            tp["minmass"] = max(1.0, float(minmass))
        if percentile is not None:
            tp["percentile"] = float(max(0.0, min(100.0, percentile)))
        if search_range is not None:
            tp["search_range"] = max(1, int(search_range))
        if memory is not None:
            tp["memory"] = max(0, int(memory))
        merged = pipeline.track_params(ctx)
        nb.tool("set_tracking_params", {k: v for k, v in tp.items()}, "updated")
        return "tracking params now: " + json.dumps(
            {k: merged[k] for k in ("diameter", "minmass", "percentile",
                                    "search_range", "memory")})

    @tool
    def reprocess_clip(clip_id: str) -> str:
        """Re-run bead detection + linking on an ALREADY-RECORDED clip using the
        current tracking params (see set_tracking_params), rewriting its CSV. Returns
        the bead-count change. Follow with reanalyze() to update the viscosity."""
        clip = next((c for c in state.get("clips", []) if c["clip_id"] == clip_id), None)
        if clip is None:
            return f"no such clip {clip_id}"
        old_n = clip.get("n_beads_total")
        pipeline.track_clip(ctx, clip)              # mutates clip in place, rewrites CSV
        prepare_workspace(ctx, state)              # refresh staged ./data copies
        new_n = clip.get("n_beads_total")
        nb.tool("reprocess_clip", {"clip_id": clip_id}, f"beads {old_n} -> {new_n}")
        return (f"reprocessed {clip_id}: linked beads {old_n} -> {new_n}. "
                f"Call reanalyze() to fold this into the viscosity estimate.")

    @tool
    def set_analysis_params(fit_fraction: float = None, drift_correction: bool = None,
                            min_coverage: float = None, max_ecc: float = None) -> str:
        """Re-tune the ANALYSIS parameters. drift_correction=False disables linear
        drift subtraction (use if the MSD intercept warnings say drift is being
        over-corrected on a drift-free sample); fit_fraction changes how much of the
        MSD curve is fit; min_coverage/max_ecc change which beads pass QC. Call
        reanalyze() afterwards to apply them. Only the params you pass change."""
        c = ctx.cfg
        changed = {}
        if fit_fraction is not None:
            c.fit_fraction = float(max(0.05, min(1.0, fit_fraction))); changed["fit_fraction"] = c.fit_fraction
        if drift_correction is not None:
            c.drift_correction = bool(drift_correction); changed["drift_correction"] = c.drift_correction
        if min_coverage is not None:
            c.min_coverage = float(max(0.1, min(1.0, min_coverage))); changed["min_coverage"] = c.min_coverage
        if max_ecc is not None:
            c.max_ecc = float(max(0.0, min(1.0, max_ecc))); changed["max_ecc"] = c.max_ecc
        nb.tool("set_analysis_params", changed, "updated")
        return "analysis params now: " + json.dumps({
            "fit_fraction": c.fit_fraction, "drift_correction": c.drift_correction,
            "min_coverage": c.min_coverage, "max_ecc": c.max_ecc})

    @tool
    def reanalyze() -> str:
        """Re-run QC + viscosity estimation across ALL recorded clips with the current
        analysis params and COMMIT the new results/aggregate (this is what the report
        and dashboard will show). Returns the before/after viscosity so you can judge
        whether the change helped. This changes real state -- use it to actually fix a
        wrong result, not just to explore."""
        old = _eta(state.get("aggregate"))
        datasets, results, agg = pipeline.reanalyze_all(
            ctx, state.get("clips", []), state.get("um_per_px"))
        state["datasets"], state["results"], state["aggregate"] = datasets, results, agg
        prepare_workspace(ctx, state)
        nb.result(f"reanalyze: {old} -> {_eta(agg)}", aggregate=agg)
        return f"reanalyzed all clips. viscosity {old} -> {_eta(agg)} ({len(results)} beads)"

    return [write_file, read_file, list_files, run_python,
            read_results_summary, read_tracks_head,
            set_tracking_params, reprocess_clip, set_analysis_params, reanalyze]
