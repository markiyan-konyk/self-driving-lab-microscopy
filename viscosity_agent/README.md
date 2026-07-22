# viscosity_agent — autonomous AI-scientist for Brownian-motion viscometry

An autonomous agent that measures the **viscosity of a fluid** by watching the
Brownian motion of silica (SiO₂) microspheres under the SCOPIO microscope,
tracking them, fitting the mean-squared displacement, and applying the
Stokes–Einstein relation — then **critiquing its own results** and writing a lab
report. It is a standalone **API client** of the microscope's gateway (like
[`ui/`](../ui/) and [`galvo_draw/`](../galvo_draw/)): no ROS, no Docker.

Built with **LangGraph**, provider-agnostic — runs on **either** an Anthropic or
an OpenAI key. The **only** human input is the microscope calibration
(micrometres per pixel); everything else — surveying the sample, choosing where
to record, acquiring clips, tracking, statistics, deciding whether to gather more
data, and reporting — is autonomous.

```
connect → calibration_gate → setup_camera → survey ⇄ decide_scene ─(acquire)→
    acquire → process → qc → analyze → critique ─(converged)→ report
                 ▲                          │
                 └──(acquire_more)──────────┤
              survey ◄──(move_fov)──────────┘
```

## What it does that the manual pipeline doesn't

- **Real timestamps.** The offline pipeline fabricates `timestamp_ms = frame/fps`
  from a nominal fps; the agent stamps every frame with its true arrival time, so
  the diffusion fit uses real (jittery) timing — which the MSD `τ = mean(t[lag:] −
  t[:-lag])` handles exactly.
- **Client-side scene assessment.** The Pi's `tracker_node` is idle (no trackpy in
  the ROS image), so bead detection runs here — a fast single-frame trackpy detect
  with the auto-tuned percentile (the one efficiency knob) to read bead count and
  clumping before committing to a recording.
- **Self-critique with a sandbox.** After analysis the agent can write and run its
  own Python (MSD-linearity plots, fit-fraction sensitivity, outlier checks) in a
  jailed workspace before deciding whether the estimate is trustworthy.
- **Closed-loop self-tuning.** If it diagnoses a problem, it can *fix the method and
  commit the change* without re-recording: re-tune trackpy detection/linking and
  re-process a saved clip (`set_tracking_params` → `reprocess_clip`), or change the
  analysis (`set_analysis_params` → `reanalyze`, e.g. disable drift correction when
  the MSD intercept shows it's over-corrected) — the report and dashboard then show
  the corrected result. It's told to only keep a change that actually helps.
- **Live dashboard.** A web view of every decision, tool call, and the running
  viscosity estimate vs the literature value — plus **live token spend and USD
  cost** so you always know what a run is costing.

It reuses the repo's validated physics in [`viscosity/`](../viscosity/)
(`track.py`, `calculation.py`, `main.py`) via a thin import shim.

## Install

```bash
cd viscosity_agent
pip install -r requirements.txt      # includes -e ../scopio_client
cp .env.example .env                 # then edit
```

## Run

```bash
# No hardware, no API key — deterministic heuristics on a synthetic sample with a
# KNOWN true viscosity (recovers it within ~10%). Great first smoke test:
python run_agent.py --dry-run --offline

# No hardware, but the real LLM drives every decision (needs an LLM key):
python run_agent.py --dry-run

# The real thing (needs SCOPIO_URL/KEY + an LLM key). One prompt: calibration.
python run_agent.py
```

Useful flags: `--provider anthropic|openai`, `--model <id>`, `--um-per-px 0.5`,
`--max-iterations N`, `--clip-seconds N`, `--no-dashboard`.

Each run writes `runs/<timestamp>/`:

| file | what |
|---|---|
| `report.md` | the final lab report (result ± uncertainty, literature comparison, anomalies, confidence) |
| `notebook.md` / `notebook.jsonl` | full trace of every decision, tool call, and result |
| `state.json` | live snapshot the dashboard reads |
| `clips/clip_NNN/` | frames + `timestamps.csv` (real arrival times) + `clip_meta.json` |
| `csvs/clip_NNN.csv` | tracking table with real timestamps |
| `workspace/` | the agent's own analysis scripts + plots |
| `snapshots/` | annotated scene previews |

## Dashboard

Auto-launched at `http://localhost:8070` (disable with `--no-dashboard`), or point
it at any finished run:

```bash
python -m agent.dashboard --run-dir runs/<timestamp>
```

## Models

Defaults are the current flagships — Anthropic `claude-opus-4-8`, OpenAI
`gpt-5.6-sol` — auto-selected from whichever API key is present. Override with
`--provider` / `--model` or `ANTHROPIC_MODEL` / `OPENAI_MODEL`. (Sampling params
like temperature are never sent — Opus 4.8 rejects them, and this keeps behaviour
identical across providers.)

**Token spend & cost.** Every LLM call's tokens are metered (via LangChain's
`usage_metadata`) and priced from a built-in per-model table, broken down by node.
Totals show live on the dashboard, in the run summary, and in `report.md`. If the
prices drift, override them per run with `PRICE_IN_PER_MTOK` / `PRICE_OUT_PER_MTOK`
(or the same keys in `config.yaml`).

## Configuration

All experiment/analysis/budget knobs live in [`config.yaml`](config.yaml)
(temperature, bead radius, coverage/eccentricity filters, clip bounds, iteration
and wall-clock budgets, …). Any of them can be overridden by an environment
variable of the same name in upper case. Secrets come only from the environment /
`.env`.

## Safety / operating notes

- The stage and camera have a single owner — don't run the agent while the UI is
  jogging the stage.
- Wrong calibration scales the viscosity by the **square** of the error; the
  report prints the µm/px used prominently.
- Loops are hard-bounded (iterations, survey moves, wall-clock, and a LangGraph
  recursion limit), so a run always terminates at a report.

## Layout

```
run_agent.py            entry point
config.yaml             all knobs
agent/
  config.py  llm.py  state.py  context.py   prompts.py
  graph.py   nodes.py                        (orchestration)
  notebook.py  dashboard.py  mock_scope.py
  _viscosity_import.py                       (shim onto ../viscosity)
  tools/  instrument.py vision.py acquisition.py pipeline.py sandbox.py
```
