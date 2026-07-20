"""The graph's nodes.

Each node is ``fn(ctx, state) -> dict`` returning a partial state update; graph.py
binds ``ctx`` and wires them together. Deterministic nodes drive the instrument
and the analysis pipeline; three nodes (decide_scene, critique, report) call the
LLM -- and each of those has an ``offline`` heuristic twin so the entire graph can
run with no API key (for CI, demos, and verification).

Routers (also here) read the latest structured decision plus the hard budgets and
return the next node's name, guaranteeing the run always terminates at ``report``.
"""

import json
import sys
import time

from .context import Context
from .prompts import CRITIQUE, DECIDE_SCENE, REPORT, SYSTEM
from .state import Critique, SceneDecision
from .tools import acquisition, instrument, pipeline, sandbox, vision

_MAX_CRITIQUE_STEPS = 8       # tool-call turns the critique agent may take


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
def _commit(ctx: Context, state: dict, update: dict) -> dict:
    """Refresh the dashboard state snapshot from the merged state, return update."""
    ctx.nb.set_state({**state, **update})
    return update


def over_budget(ctx: Context, state: dict) -> bool:
    return (time.monotonic() - state["started_at"]) > ctx.cfg.wall_clock_budget_s


def _text(resp) -> str:
    c = getattr(resp, "content", resp)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict):
                out.append(b.get("text", "") or "")
            else:
                out.append(str(b))
        return "".join(out)
    return str(c)


def _last_clip(state):
    return state["clips"][-1] if state.get("clips") else None


def _fmt_aggregate(agg) -> str:
    if not agg:
        return "none yet"
    wm, wu = agg.get("weighted_mean_Pa_s"), agg.get("weighted_unc_Pa_s")
    return (f"{wm:.4e} +/- {wu:.2e} Pa.s (weighted, N={agg.get('n_particles')}); "
            f"mean {agg.get('mean_Pa_s'):.4e}, SEM {agg.get('sem_Pa_s'):.2e}")


def _fmt_results_table(results) -> str:
    if not results:
        return "  (no beads yet)"
    lines = ["  clip        id     eta[Pa.s]      +/-        D[m2/s]     R2   pts  warn"]
    for r in results[-30:]:
        lines.append(
            f"  {r['clip_id']:<10} {r['particle']:>3}  "
            f"{r['viscosity_Pa_s']:.3e}  {r['viscosity_uncertainty_Pa_s']:.2e}  "
            f"{r['diffusion_m2_s']:.3e}  {r['fit_r2']:.3f}  {r['num_points']:>4}  "
            f"{'!' if r.get('intercept_warning') else ''}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  deterministic nodes                                                         #
# --------------------------------------------------------------------------- #
def connect(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("connect")
    h = instrument.get_health(ctx)
    controls = instrument.get_camera_controls(ctx)
    ok = bool(h.get("result", {}).get("ok", False)) if h.get("ok") else False
    status = "connected" if ok else "connect-degraded"
    ctx.nb.note(f"health ok={ok}")
    return _commit(ctx, state, {"camera_controls": controls or {}, "status": status})


def calibration_gate(ctx: Context, state: dict) -> dict:
    """Obtain the one human input: micrometres/pixel. Every other step is autonomous."""
    ctx.nb.phase("calibration_gate")
    cal = instrument.get_calibration(ctx)
    if cal.get("has_um_per_px"):
        val = float(cal["um_per_px"])
        ctx.nb.note(f"using calibration already on the gateway: {val:g} um/px")
        return _commit(ctx, state, {"um_per_px": val, "calibration_source": "gateway",
                                    "status": "calibrated"})

    # not on the gateway -- take it from CLI/config, else prompt the human (once)
    val = state.get("um_per_px") or ctx.cfg.um_per_px
    source = "config"
    if not val:
        val = _prompt_um_per_px(ctx)
        source = "prompt"
    if not val:
        ctx.nb.error("no calibration available; cannot measure viscosity")
        return _commit(ctx, state, {"abort_reason": "no calibration provided",
                                    "status": "aborted"})
    instrument.set_calibration(ctx, val)
    ctx.nb.note(f"calibration set to {val:g} um/px (source: {source})")
    return _commit(ctx, state, {"um_per_px": float(val),
                                "calibration_source": source, "status": "calibrated"})


def _prompt_um_per_px(ctx: Context):
    if not sys.stdin or not sys.stdin.isatty():
        return None
    try:
        raw = input("\n>>> Microscope calibration required. "
                    "Enter micrometres per pixel (e.g. 0.5): ").strip()
        return float(raw) if raw else None
    except (EOFError, ValueError, KeyboardInterrupt):
        return None


def setup_camera(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("setup_camera")
    instrument.set_camera(ctx, framerate=float(ctx.cfg.target_fps))
    controls = instrument.get_camera_controls(ctx)
    return _commit(ctx, state, {"camera_controls": controls or state.get("camera_controls", {}),
                                "status": "camera-ready"})


def survey(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("survey")
    scene = vision.assess_scene(ctx, n_frames=5)
    hist = list(state.get("fov_history", []))
    hist.append({"pos": scene.get("stage_pos"), "bead_count": scene["bead_count"],
                 "clump_fraction": scene["clump_fraction"],
                 "focus_score": scene["focus_score"], "snapshot": scene.get("snapshot")})
    return _commit(ctx, state, {"scene": scene, "fov_history": hist, "status": "surveying"})


def decide_scene(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("decide_scene")
    scene = state.get("scene") or {}
    if ctx.offline:
        decision = _decide_scene_offline(ctx, state, scene)
    else:
        decision = _decide_scene_llm(ctx, state, scene)
    ctx.nb.decision("scene_decision", decision)
    return _commit(ctx, state, {"scene_decision": decision, "status": "deciding-scene"})


def _decide_scene_llm(ctx: Context, state: dict, scene: dict) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage
    cfg = ctx.cfg
    prompt = DECIDE_SCENE.format(
        scene=json.dumps(scene, indent=2),
        min_scene_beads=cfg.min_scene_beads, max_clump_fraction=cfg.max_clump_fraction,
        focus_floor=cfg.focus_floor, survey_attempts=state.get("survey_attempts", 0),
        max_fov_moves=cfg.max_fov_moves,
        fov_history=json.dumps(state.get("fov_history", [])[-5:], indent=2),
        default_clip_s=cfg.default_clip_s, clip_min_s=cfg.clip_min_s,
        clip_max_s=cfg.clip_max_s, max_jog_steps=cfg.max_jog_steps, jog_hint=300)
    llm = ctx.llm.with_structured_output(SceneDecision)
    try:
        out: SceneDecision = llm.invoke(
            [SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
        return out.model_dump()
    except Exception as e:              # never let a bad LLM call stall the graph
        ctx.nb.error(f"decide_scene LLM failed ({e}); falling back to heuristic")
        return _decide_scene_offline(ctx, state, scene)


def _decide_scene_offline(ctx: Context, state: dict, scene: dict) -> dict:
    cfg = ctx.cfg
    n = scene.get("bead_count", 0)
    clump = scene.get("clump_fraction", 0.0)
    if n >= cfg.min_scene_beads and clump <= cfg.max_clump_fraction:
        return SceneDecision(
            action="acquire", clip_duration_s=float(cfg.default_clip_s),
            reasoning=f"{n} beads (>= {cfg.min_scene_beads}) and clump "
                      f"{clump:.0%} (<= {cfg.max_clump_fraction:.0%}): good FOV, "
                      f"recording {cfg.default_clip_s}s.").model_dump()
    step = 300 if state.get("survey_attempts", 0) % 2 == 0 else -300
    return SceneDecision(
        action="jog", jog_dx=step, jog_dy=0,
        reasoning=f"only {n} beads / clump {clump:.0%}; jogging {step} steps to "
                  f"find a denser, cleaner field.").model_dump()


def act_on_scene(ctx: Context, state: dict) -> dict:
    """Apply a non-acquire scene action (jog / autofocus / etc.), then loop to survey."""
    d = state.get("scene_decision") or {}
    action = d.get("action")
    attempts = state.get("survey_attempts", 0)
    if action == "jog":
        instrument.jog_stage(ctx, dx=d.get("jog_dx", 0), dy=d.get("jog_dy", 0))
        attempts += 1
    elif action == "autofocus":
        instrument.autofocus(ctx)
    elif action == "white_balance":
        instrument.white_balance(ctx)
    elif action == "adjust_camera":
        instrument.set_camera(ctx, **(d.get("camera_updates") or {}))
    return _commit(ctx, state, {"survey_attempts": attempts, "status": "adjusting"})


def relocate(ctx: Context, state: dict) -> dict:
    """Move to a fresh field of view after the critique asked for one, then survey."""
    ctx.nb.phase("relocate")
    step = 350 if state.get("iteration", 0) % 2 == 0 else -350
    instrument.jog_stage(ctx, dx=step, dy=step)
    return _commit(ctx, state, {"survey_attempts": 0, "status": "relocating"})


def acquire(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("acquire")
    d = state.get("scene_decision") or {}
    duration = d.get("clip_duration_s") or ctx.cfg.default_clip_s
    pos = (state.get("scene") or {}).get("stage_pos")
    clip = acquisition.record_clip(ctx, float(duration), stage_pos=pos)
    clips = list(state.get("clips", [])) + [clip]
    # a fresh acquire/analyse cycle -- reset the per-cycle survey counter
    return _commit(ctx, state, {"clips": clips, "survey_attempts": 0, "status": "acquired"})


def process(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("process")
    clips = list(state.get("clips", []))
    if clips:
        clips[-1] = pipeline.track_clip(ctx, clips[-1])
    return _commit(ctx, state, {"clips": clips, "status": "processed"})


def qc(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("qc")
    clip = _last_clip(state)
    datasets = list(state.get("datasets", []))
    if clip:
        datasets.append(pipeline.qc_tracks(ctx, clip))
    return _commit(ctx, state, {"datasets": datasets, "status": "qc-done"})


def analyze(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("analyze")
    clip = _last_clip(state)
    dataset = state["datasets"][-1] if state.get("datasets") else None
    results = list(state.get("results", []))
    if clip and dataset:
        new = pipeline.estimate_all(ctx, clip, dataset, state["um_per_px"])
        results.extend(new)
    agg = pipeline.aggregate_results(results)
    iteration = state.get("iteration", 0) + 1
    if agg:
        ctx.nb.result("aggregate " + _fmt_aggregate(agg), aggregate=agg)
    return _commit(ctx, state, {"results": results, "aggregate": agg,
                                "iteration": iteration, "status": "analyzed"})


# --------------------------------------------------------------------------- #
#  critique (LLM + sandbox tool loop, with offline twin)                       #
# --------------------------------------------------------------------------- #
def critique(ctx: Context, state: dict) -> dict:
    ctx.nb.phase("critique")
    if ctx.offline:
        crit = _critique_offline(ctx, state)
    else:
        crit = _critique_llm(ctx, state)
    ctx.nb.decision("critique", crit)
    return _commit(ctx, state, {"critique": crit, "status": "critiqued"})


def _critique_prompt(ctx: Context, state: dict) -> str:
    cfg = ctx.cfg
    return CRITIQUE.format(
        iteration=state.get("iteration", 0), max_iterations=cfg.max_iterations,
        n_clips=len(state.get("clips", [])), n_results=len(state.get("results", [])),
        aggregate=_fmt_aggregate(state.get("aggregate")),
        literature_eta_Pa_s=cfg.literature_eta_Pa_s, um_per_px=state.get("um_per_px"),
        results_table=_fmt_results_table(state.get("results", [])),
        min_beads=cfg.min_beads, target_beads=cfg.target_beads)


def _critique_llm(ctx: Context, state: dict) -> dict:
    from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage,
                                         ToolMessage)
    tools = sandbox.build_analysis_tools(ctx, state)
    tool_map = {t.name: t for t in tools}
    llm_tools = ctx.llm.bind_tools(tools)

    messages = [SystemMessage(content=SYSTEM),
                HumanMessage(content=_critique_prompt(ctx, state))]
    try:
        for _ in range(_MAX_CRITIQUE_STEPS):
            resp = llm_tools.invoke(messages)
            messages.append(resp)
            calls = getattr(resp, "tool_calls", None) or []
            if not calls:
                break
            for call in calls:
                tool = tool_map.get(call["name"])
                if tool is None:
                    result = f"unknown tool {call['name']}"
                else:
                    try:
                        result = tool.invoke(call["args"])
                    except Exception as e:
                        result = f"tool error: {e}"
                messages.append(ToolMessage(content=str(result),
                                            tool_call_id=call["id"]))
        verdict = ctx.llm.with_structured_output(Critique).invoke(
            messages + [HumanMessage(content="Now give your final structured verdict.")])
        return verdict.model_dump()
    except Exception as e:
        ctx.nb.error(f"critique LLM failed ({e}); falling back to heuristic")
        return _critique_offline(ctx, state)


def _critique_offline(ctx: Context, state: dict) -> dict:
    cfg = ctx.cfg
    agg = state.get("aggregate")
    n = agg.get("n_particles", 0) if agg else 0
    concerns = []
    if not agg or n == 0:
        return Critique(verdict="acquire_more", confidence=0.1,
                        reasoning="no beads estimated yet; need data.",
                        concerns=["no results"]).model_dump()
    wm = agg["weighted_mean_Pa_s"]
    wu = agg["weighted_unc_Pa_s"]
    rel = (wu / wm) if wm else float("inf")
    dev = abs(wm - cfg.literature_eta_Pa_s) / cfg.literature_eta_Pa_s
    if rel > 0.15:
        concerns.append(f"relative uncertainty {rel:.0%} is high")
    if dev > 0.25:
        concerns.append(f"{dev:.0%} from the literature value")
    if n >= cfg.target_beads and rel <= 0.15:
        verdict, conf = "converged", 0.85
        reason = (f"{n} beads, weighted eta {wm:.3e} +/- {wu:.1e} Pa.s "
                  f"(rel unc {rel:.0%}), {dev:.0%} from literature: trustworthy.")
    elif n >= cfg.min_beads:
        verdict, conf = "converged", 0.7
        reason = (f"{n} beads (>= {cfg.min_beads}); eta {wm:.3e} Pa.s, "
                  f"{dev:.0%} from literature. Adequate; converging.")
    elif state.get("iteration", 0) < cfg.max_iterations:
        verdict, conf = "acquire_more", 0.4
        reason = f"only {n} beads (< {cfg.min_beads}); record another clip."
    else:
        verdict, conf = "converged", 0.5
        reason = f"budget exhausted with {n} beads; reporting best estimate."
    return Critique(verdict=verdict, confidence=conf, reasoning=reason,
                    concerns=concerns).model_dump()


# --------------------------------------------------------------------------- #
#  report                                                                      #
# --------------------------------------------------------------------------- #
def report(ctx: Context, state: dict) -> dict:
    import os
    ctx.nb.phase("report")
    path = os.path.join(ctx.run_dir, "report.md")
    if ctx.offline or ctx.llm is None:
        md = _report_offline(ctx, state)
    else:
        md = _report_llm(ctx, state)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    ctx.nb.result(f"report written -> {os.path.basename(path)}",
                  report_path=os.path.relpath(path, ctx.run_dir))
    return _commit(ctx, state, {"report_path": path, "status": "done"})


def _run_json(ctx: Context, state: dict) -> str:
    trimmed = {
        "provider_model": state.get("provider_model"),
        "dry_run": state.get("dry_run"),
        "um_per_px": state.get("um_per_px"),
        "calibration_source": state.get("calibration_source"),
        "temperature_K": ctx.cfg.temperature_K,
        "bead_radius_m": ctx.cfg.bead_radius_m,
        "literature_eta_Pa_s": ctx.cfg.literature_eta_Pa_s,
        "aggregate": state.get("aggregate"),
        "clips": [{k: c.get(k) for k in ("clip_id", "n_frames", "measured_fps",
                                         "fps_jitter_pct", "n_beads_total",
                                         "stage_pos")} for c in state.get("clips", [])],
        "datasets": state.get("datasets", []),
        "results": state.get("results", []),
        "fov_history": state.get("fov_history", []),
        "iterations": state.get("iteration"),
        "abort_reason": state.get("abort_reason"),
    }
    return json.dumps(trimmed, indent=2, default=str)


def _report_llm(ctx: Context, state: dict) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage
    cfg = ctx.cfg
    crit = state.get("critique") or {}
    prompt = REPORT.format(
        literature_eta_Pa_s=cfg.literature_eta_Pa_s, um_per_px=state.get("um_per_px"),
        temperature_K=cfg.temperature_K, bead_radius_m_um=cfg.bead_radius_m * 1e6,
        verdict=crit.get("verdict"), confidence=crit.get("confidence"),
        run_json=_run_json(ctx, state), critique=json.dumps(crit, indent=2))
    try:
        resp = ctx.llm.invoke([SystemMessage(content=SYSTEM),
                               HumanMessage(content=prompt)])
        return _text(resp)
    except Exception as e:
        ctx.nb.error(f"report LLM failed ({e}); writing templated report")
        return _report_offline(ctx, state)


def _report_offline(ctx: Context, state: dict) -> str:
    cfg = ctx.cfg
    agg = state.get("aggregate")
    crit = state.get("critique") or {}
    lines = ["# Autonomous Viscosity Measurement — Report", ""]
    if agg:
        wm, wu = agg["weighted_mean_Pa_s"], agg["weighted_unc_Pa_s"]
        dev = (wm - cfg.literature_eta_Pa_s) / cfg.literature_eta_Pa_s * 100
        lines += [
            f"**Result:** η = {wm:.3e} ± {wu:.1e} Pa·s "
            f"({wm * 1e3:.3f} ± {wu * 1e3:.3f} mPa·s), inverse-variance weighted "
            f"over N = {agg['n_particles']} beads across "
            f"{len(state.get('clips', []))} clip(s).", "",
            f"**Literature (water @ 25 °C):** {cfg.literature_eta_Pa_s:.3e} Pa·s "
            f"→ deviation {dev:+.1f}%.", "",
            f"- mean {agg['mean_Pa_s']:.3e} Pa·s, std {agg['std_Pa_s']:.2e}, "
            f"SEM {agg['sem_Pa_s']:.2e}", ""]
    else:
        lines += ["**Result:** no viscosity could be estimated "
                  f"({state.get('abort_reason') or 'insufficient data'}).", ""]
    lines += [
        "## Method", "",
        "Brownian-motion tracking of SiO₂ beads; 2-D MSD(τ) = 4·D·τ fit, "
        "Stokes–Einstein η = k_BT / (6πrD). Real per-frame arrival timestamps were "
        "used (not a nominal fps).", "",
        f"- calibration: **{state.get('um_per_px')} µm/px** "
        f"(source: {state.get('calibration_source')}) — note η scales with the "
        f"square of any calibration error",
        f"- temperature: {cfg.temperature_K} K; bead radius: {cfg.bead_radius_m * 1e6:g} µm",
        f"- provider/model: {state.get('provider_model')}"
        + ("  ·  dry-run (synthetic scope)" if state.get("dry_run") else ""), ""]
    lines += ["## Per-bead results", "", "```", _fmt_results_table(state.get("results", [])),
              "```", ""]
    lines += ["## Self-critique", "",
              f"- verdict: **{crit.get('verdict')}** (confidence {crit.get('confidence')})",
              f"- reasoning: {crit.get('reasoning')}"]
    for c in crit.get("concerns", []):
        lines.append(f"- concern: {c}")
    lines += ["", "## Limitations", "",
              "- MJPEG frames are timestamped at network arrival, not sensor "
              "exposure — adds timing jitter (handled by the irregular-lag MSD) "
              "but not a bias.",
              "- Calibration accuracy dominates systematic error (quadratic in µm/px).",
              "- " + ("Synthetic dry-run data." if state.get("dry_run")
                      else "Single-session sampling; more fields of view would tighten the estimate."),
              ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  routers                                                                     #
# --------------------------------------------------------------------------- #
def route_after_calibration(ctx: Context, state: dict) -> str:
    return "abort" if state.get("abort_reason") else "ok"


def route_scene(ctx: Context, state: dict) -> str:
    """acquire | act | report(abort). Falls back to acquire when survey budget spent."""
    d = state.get("scene_decision") or {}
    action = d.get("action", "jog")
    if over_budget(ctx, state):
        ctx.nb.note("wall-clock budget reached during survey -> acquiring here")
        return "acquire"
    if action == "abort":
        return "abort"
    if action == "acquire":
        return "acquire"
    # jog / autofocus / white_balance / adjust_camera
    if state.get("survey_attempts", 0) >= ctx.cfg.max_fov_moves:
        ctx.nb.note(f"survey move budget ({ctx.cfg.max_fov_moves}) spent "
                    f"-> acquiring at the current field of view")
        return "acquire"
    return "act"


def route_after_acquire(ctx: Context, state: dict) -> str:
    clip = _last_clip(state)
    if not clip or clip.get("n_frames", 0) < 20:
        # capture failed -- either retry via survey or give up on budget
        if over_budget(ctx, state) or state.get("iteration", 0) >= ctx.cfg.max_iterations:
            return "report"
        return "survey"
    return "process"


def route_critique(ctx: Context, state: dict) -> str:
    crit = state.get("critique") or {}
    verdict = crit.get("verdict", "converged")
    if verdict in ("converged", "abort"):
        return "report"
    if over_budget(ctx, state):
        ctx.nb.note("wall-clock budget reached -> reporting")
        return "report"
    if state.get("iteration", 0) >= ctx.cfg.max_iterations:
        ctx.nb.note(f"iteration budget ({ctx.cfg.max_iterations}) reached -> reporting")
        return "report"
    if verdict == "acquire_more":
        return "acquire"
    return "relocate"      # move_fov
