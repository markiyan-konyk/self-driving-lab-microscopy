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

_MAX_CRITIQUE_STEPS = 12      # tool-call turns the critique agent may take
                              # (analysis scripts + re-tuning both cost turns)


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
def _commit(ctx: Context, state: dict, update: dict) -> dict:
    """Refresh the dashboard state snapshot from the merged state, return update."""
    if ctx.usage is not None:
        update["usage"] = ctx.usage.snapshot()
    ctx.nb.set_state({**state, **update})
    return update


def _log_usage(ctx: Context, node: str, before: int):
    """Emit a compact token/cost line to the feed after an LLM node."""
    if ctx.usage is None:
        return
    snap = ctx.usage.snapshot()
    delta = snap["total_tokens"] - before
    cost = f"${snap['cost_usd']:.4f}" if snap["priced"] else "price unset"
    ctx.nb.note(f"tokens: +{delta:,} this step; run total {snap['total_tokens']:,} "
                f"({cost})", usage=snap)


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
    reachable = bool(h.get("ok"))          # did the health call itself succeed?
    if not reachable and not state.get("dry_run"):
        err = h.get("error", "unreachable")
        msg = (f"cannot reach the microscope at {ctx.cfg.scopio_url}: {err}. "
               f"Check SCOPIO_URL / SCOPIO_API_KEY and that the Pi gateway is up "
               f"(curl {ctx.cfg.scopio_url}/api/v1/health), or use --dry-run.")
        ctx.nb.error(msg)
        print("\n  [x] " + msg + "\n")
        return _commit(ctx, state, {"abort_reason": msg, "status": "aborted"})
    controls = instrument.get_camera_controls(ctx)
    ok = bool(h.get("result", {}).get("ok", False)) if reachable else False
    status = "connected" if ok else "connect-degraded"
    ctx.nb.note(f"health reachable={reachable} ok={ok}")
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
        guidance = (
            "No microscope calibration (micrometres per pixel) is available. "
            "This is the ONE value the agent needs from you. Provide it any of these "
            "ways and re-run:\n"
            "    - CLI:  python run_agent.py --um-per-px 0.5\n"
            "    - env:  set UM_PER_PX=0.5   (or add it to viscosity_agent/.env)\n"
            "    - UI :  set the calibration once in the SCOPIO web UI - it latches "
            "on the gateway and the agent then picks it up automatically.\n"
            "(0.5 is only an example - use your objective+camera's real um/px.)")
        ctx.nb.error("calibration missing - cannot measure viscosity")
        print("\n  [x] " + guidance + "\n")
        return _commit(ctx, state, {
            "abort_reason": "calibration not provided - pass --um-per-px, set "
                            "UM_PER_PX, or set it in the UI (see console)",
            "status": "aborted"})
    val = float(val)
    res = instrument.set_calibration(ctx, val)     # latch on the gateway if we can
    if not res.get("ok"):
        ctx.nb.note("could not latch calibration on the gateway; using it locally anyway")
    ctx.nb.note(f"calibration = {val:g} um/px (source: {source})")
    return _commit(ctx, state, {"um_per_px": val,
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
    before = ctx.usage.snapshot()["total_tokens"] if ctx.usage else 0
    if ctx.offline:
        decision = _decide_scene_offline(ctx, state, scene)
    else:
        decision = _decide_scene_llm(ctx, state, scene)
        _log_usage(ctx, "decide_scene", before)
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
    llm = ctx.llm.with_structured_output(SceneDecision, include_raw=True)
    try:
        res = llm.invoke([SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
        if ctx.usage is not None and res.get("raw") is not None:
            ctx.usage.record(res["raw"], node="decide_scene")
        parsed = res.get("parsed")
        if parsed is None:
            raise ValueError(res.get("parsing_error") or "no structured output")
        return parsed.model_dump()
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
    before = ctx.usage.snapshot()["total_tokens"] if ctx.usage else 0
    if ctx.offline:
        crit = _critique_offline(ctx, state)
    else:
        crit = _critique_llm(ctx, state)
        _log_usage(ctx, "critique", before)
    ctx.nb.decision("critique", crit)
    # The critique agent may have re-tuned tracking/analysis and reprocessed the
    # data in place (reprocess_clip / reanalyze). Return those committed changes so
    # they persist into the report and dashboard.
    return _commit(ctx, state, {
        "critique": crit, "status": "critiqued",
        "clips": state.get("clips", []), "datasets": state.get("datasets", []),
        "results": state.get("results", []), "aggregate": state.get("aggregate")})


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
            if ctx.usage is not None:
                ctx.usage.record(resp, node="critique")
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
        res = ctx.llm.with_structured_output(Critique, include_raw=True).invoke(
            messages + [HumanMessage(content="Now give your final structured verdict.")])
        if ctx.usage is not None and res.get("raw") is not None:
            ctx.usage.record(res["raw"], node="critique")
        verdict = res.get("parsed")
        if verdict is None:
            raise ValueError(res.get("parsing_error") or "no structured verdict")
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
    before = ctx.usage.snapshot()["total_tokens"] if ctx.usage else 0
    # An aborted run (no calibration, unreachable, etc.) has no data to reason
    # about -- write the templated report instead of paying the LLM to restate it.
    if ctx.offline or ctx.llm is None or state.get("abort_reason"):
        md = _report_offline(ctx, state)
    else:
        md = _report_llm(ctx, state)
        _log_usage(ctx, "report", before)
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
        "usage": state.get("usage"),
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
        if ctx.usage is not None:
            ctx.usage.record(resp, node="report")
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
    u = state.get("usage")
    if u and u.get("calls"):
        cost = f"${u['cost_usd']:.4f}" if u.get("priced") else "price unset"
        lines += [f"- LLM cost: **{cost}** — {u['input_tokens']:,} input + "
                  f"{u['output_tokens']:,} output tokens "
                  f"({u['total_tokens']:,} total) over {u['calls']} calls", ""]
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
def route_after_connect(ctx: Context, state: dict) -> str:
    return "abort" if state.get("abort_reason") else "ok"


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
