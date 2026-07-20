#!/usr/bin/env python3
"""viscosity_agent -- run the autonomous Brownian-motion viscometer.

An autonomous "AI scientist" that connects to the SCOPIO microscope, takes ONE
human input (the micrometres/pixel calibration), then surveys the sample, records
clips, tracks beads, runs the Stokes-Einstein physics, critiques its own results
(writing and running its own analysis scripts), decides whether to gather more
data or move, and writes a lab report -- all on its own, with a live dashboard.

Examples
--------
    # Full run against a real microscope (needs an LLM key + SCOPIO_URL/KEY):
    python run_agent.py

    # No hardware: synthetic Brownian sample with a known true viscosity:
    python run_agent.py --dry-run --um-per-px 0.5

    # No hardware AND no LLM key (deterministic heuristics -- CI / offline demo):
    python run_agent.py --dry-run --offline

Env: SCOPIO_URL, SCOPIO_API_KEY (real runs); ANTHROPIC_API_KEY or OPENAI_API_KEY
(LLM runs). See .env.example and config.yaml.
"""

import argparse
import os
import sys
import time
from datetime import datetime

# Make ``agent`` importable when run as a script from anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.config import load_settings          # noqa: E402
from agent.context import Context                # noqa: E402
from agent.llm import describe_llm, make_llm     # noqa: E402
from agent.notebook import Notebook             # noqa: E402
from agent.state import new_state               # noqa: E402


def parse_args():
    ap = argparse.ArgumentParser(
        description="Autonomous Brownian-motion viscometer (LangGraph).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="use a synthetic microscope (known true viscosity); no hardware")
    ap.add_argument("--offline", action="store_true",
                    help="no LLM: deterministic heuristics (still needs no hardware in dry-run)")
    ap.add_argument("--provider", choices=["anthropic", "openai"], default=None,
                    help="force the LLM provider (default: auto-detect from keys)")
    ap.add_argument("--model", default=None, help="override the model id")
    ap.add_argument("--um-per-px", type=float, default=None,
                    help="microscope calibration (the one human input)")
    ap.add_argument("--max-iterations", type=int, default=None,
                    help="cap on acquire->analyse->critique cycles")
    ap.add_argument("--clip-seconds", type=float, default=None,
                    help="override the default clip length")
    ap.add_argument("--no-dashboard", action="store_true", help="don't launch the web dashboard")
    ap.add_argument("--dashboard-port", type=int, default=None)
    return ap.parse_args()


def build_scope(cfg, nb):
    if cfg.dry_run:
        from agent.mock_scope import MockScope
        nb.note(f"dry-run: synthetic scope, eta_true = {cfg.literature_eta_Pa_s:.3e} Pa.s")
        return MockScope(eta_true=cfg.literature_eta_Pa_s,
                         temperature_K=cfg.temperature_K,
                         bead_radius_m=cfg.bead_radius_m)
    from scopio_client import Scopio
    if not cfg.scopio_api_key:
        raise SystemExit("Set SCOPIO_API_KEY and SCOPIO_URL (see .env.example), "
                         "or use --dry-run.")
    nb.note(f"connecting to microscope at {cfg.scopio_url}")
    return Scopio(cfg.scopio_url, api_key=cfg.scopio_api_key)


def main():
    args = parse_args()

    overrides = {
        "dry_run": True if args.dry_run else None,
        "llm_provider": args.provider,
        "um_per_px": args.um_per_px,
        "max_iterations": args.max_iterations,
        "default_clip_s": args.clip_seconds,
        "dashboard": False if args.no_dashboard else None,
        "dashboard_port": args.dashboard_port,
    }
    cfg = load_settings(**overrides)

    # dry-run conveniences: serial tracking (no MP spin-up) + a default calibration
    if cfg.dry_run:
        if str(cfg.track_workers).lower() == "auto":
            cfg.track_workers = "1"
        if not cfg.um_per_px:
            cfg.um_per_px = 0.5

    offline = args.offline
    if args.model and not offline:
        if cfg.resolve_provider() == "anthropic":
            cfg.anthropic_model = args.model
        else:
            cfg.openai_model = args.model

    # run directory + notebook
    run_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs",
                           datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    nb = Notebook(run_dir)

    provider_model = "offline-heuristics" if offline else describe_llm(cfg)
    nb.phase("run start", provider_model=provider_model, dry_run=cfg.dry_run,
             run_dir=run_dir)

    scope = build_scope(cfg, nb)
    llm = None if offline else make_llm(cfg)

    ctx = Context(cfg=cfg, scope=scope, nb=nb, run_dir=run_dir,
                  llm=llm, offline=offline)

    # dashboard (background thread)
    dash_url = None
    if cfg.dashboard:
        from agent.dashboard import launch_in_thread
        launch_in_thread(run_dir, cfg.dashboard_port)
        dash_url = f"http://localhost:{cfg.dashboard_port}"
        nb.note(f"dashboard live at {dash_url}")
        print(f"\n  ► dashboard: {dash_url}\n")

    started = time.monotonic()
    state = new_state(run_dir, provider_model, cfg.um_per_px, cfg.dry_run, started)
    nb.set_state(state)

    # run the graph
    from agent.graph import run_graph
    try:
        final = run_graph(ctx, state)
    except Exception as e:
        nb.error(f"run failed: {e}")
        raise
    finally:
        try:
            scope.close()
        except Exception:
            pass

    _print_summary(cfg, final, run_dir, dash_url)

    # keep the dashboard reachable for inspection after an interactive run
    if cfg.dashboard and not offline and sys.stdin and sys.stdin.isatty():
        print("  (dashboard still serving — press Ctrl-C to exit)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def _print_summary(cfg, state, run_dir, dash_url):
    agg = state.get("aggregate")
    print("\n" + "=" * 68)
    print("  RUN COMPLETE")
    print("=" * 68)
    if agg and agg.get("weighted_mean_Pa_s") == agg.get("weighted_mean_Pa_s"):
        wm, wu = agg["weighted_mean_Pa_s"], agg["weighted_unc_Pa_s"]
        dev = (wm - cfg.literature_eta_Pa_s) / cfg.literature_eta_Pa_s * 100
        print(f"  viscosity : {wm:.4e} +/- {wu:.2e} Pa.s "
              f"({wm * 1e3:.3f} mPa.s), N={agg['n_particles']} beads")
        print(f"  literature: {cfg.literature_eta_Pa_s:.4e} Pa.s  "
              f"(deviation {dev:+.1f}%)")
        if cfg.dry_run:
            print(f"  dry-run   : true eta was {cfg.literature_eta_Pa_s:.4e} Pa.s "
                  f"-> recovered within {abs(dev):.1f}%")
    else:
        print(f"  no viscosity estimated ({state.get('abort_reason') or 'see notebook'})")
    crit = state.get("critique") or {}
    print(f"  verdict   : {crit.get('verdict')} (confidence {crit.get('confidence')})")
    print(f"  report    : {state.get('report_path')}")
    print(f"  notebook  : {os.path.join(run_dir, 'notebook.md')}")
    if dash_url:
        print(f"  dashboard : {dash_url}")
    print("=" * 68 + "\n")


if __name__ == "__main__":
    main()
