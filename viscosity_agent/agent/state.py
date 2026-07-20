"""The graph's shared state + the two structured LLM decision schemas.

State is kept as plain JSON-able values (dicts / lists / scalars) so it can be
logged to the notebook and served to the dashboard verbatim. Non-serialisable
runtime objects (the live scope, the LLM, the notebook, config) are NOT in the
state -- they live on the ``Context`` passed to the nodes (see graph.py).

Record shapes (all plain dicts, documented here for reference):

    SceneMetrics = {
        "bead_count": int, "clump_fraction": float, "focus_score": float,
        "measured_fps": float, "mean_nn_dist_px": float | None,
        "percentile": float, "stage_pos": {"x","y","z"} | None,
        "snapshot": str | None,   # path to an annotated preview JPEG
    }
    ClipRecord = {
        "clip_id": str, "dir": str, "duration_s": float, "n_frames": int,
        "measured_fps": float, "fps_jitter_pct": float,
        "stage_pos": {...} | None, "csv_path": str | None,
    }
    DatasetRecord = {
        "clip_id": str, "n_beads_total": int, "n_beads_kept": int,
        "min_coverage": float, "max_ecc": float,
    }
    BeadResult = {
        "clip_id": str, "particle": int, "viscosity_Pa_s": float,
        "viscosity_uncertainty_Pa_s": float, "diffusion_m2_s": float,
        "fit_r2": float, "num_points": int, "intercept_warning": str | None,
    }
    Aggregate = {
        "n_particles": int, "mean_Pa_s": float, "std_Pa_s": float,
        "sem_Pa_s": float, "weighted_mean_Pa_s": float,
        "weighted_unc_Pa_s": float,
    }
"""

from typing import List, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
#  Structured LLM outputs (used with llm.with_structured_output)               #
# --------------------------------------------------------------------------- #
class SceneDecision(BaseModel):
    """What to do about the current field of view, decided from scene metrics."""

    action: Literal["acquire", "jog", "autofocus", "white_balance",
                    "adjust_camera", "abort"] = Field(
        description="acquire = record a clip here; jog = move the stage to a new "
                    "field of view; autofocus = run the backend autofocus sweep; "
                    "white_balance = one-shot AWB; adjust_camera = change "
                    "framerate/exposure/gain; abort = give up (sample unusable).")
    reasoning: str = Field(
        description="One or two sentences justifying the action from the metrics.")
    jog_dx: int = Field(
        default=0, description="Stage jog in X (Sangaboard steps) if action=jog.")
    jog_dy: int = Field(
        default=0, description="Stage jog in Y (Sangaboard steps) if action=jog.")
    clip_duration_s: Optional[float] = Field(
        default=None, description="Requested clip length in seconds if action=acquire.")
    camera_updates: Optional[dict] = Field(
        default=None, description="Camera controls to set if action=adjust_camera, "
                                  "e.g. {\"framerate\": 30, \"exposure\": 8000}.")


class Critique(BaseModel):
    """The self-critique verdict after analysing the accumulated results."""

    verdict: Literal["converged", "acquire_more", "move_fov", "abort"] = Field(
        description="converged = the estimate is trustworthy, stop and report; "
                    "acquire_more = record another clip at the current FOV to add "
                    "beads / reduce uncertainty; move_fov = the current FOV is "
                    "exhausted or poor, survey a new one; abort = the data cannot "
                    "yield a credible viscosity, stop and report the failure.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0-1 confidence that the current aggregate viscosity is correct.")
    reasoning: str = Field(
        description="Explanation citing the numbers: N beads, SEM, R^2, agreement "
                    "with the literature value, any intercept/drift warnings.")
    concerns: List[str] = Field(
        default_factory=list,
        description="Specific issues that could bias the result (short phrases).")


# --------------------------------------------------------------------------- #
#  Graph state                                                                 #
# --------------------------------------------------------------------------- #
class AgentState(TypedDict, total=False):
    # run identity / environment
    run_dir: str
    provider_model: str
    dry_run: bool

    # the one human input + where it came from
    um_per_px: Optional[float]
    calibration_source: str          # "gateway" | "config" | "prompt" | "mock"

    # live scene
    camera_controls: dict
    scene: Optional[dict]            # latest SceneMetrics
    fov_history: List[dict]          # every surveyed position + its metrics
    scene_decision: Optional[dict]   # latest SceneDecision (as dict)

    # data pipeline
    clips: List[dict]                # ClipRecord list
    datasets: List[dict]             # DatasetRecord list (post-QC)
    results: List[dict]              # BeadResult list (all beads, all clips)
    aggregate: Optional[dict]        # running Aggregate
    critique: Optional[dict]         # latest Critique (as dict)

    # progress / budget
    iteration: int                   # completed acquire->critique cycles
    survey_attempts: int             # jogs taken in the current survey
    started_at: float                # time.monotonic() at run start
    status: str                      # human phase label (dashboard)
    abort_reason: Optional[str]

    # output
    report_path: Optional[str]
    errors: List[str]


def new_state(run_dir: str, provider_model: str, um_per_px, dry_run: bool,
              started_at: float) -> AgentState:
    """Initial state at the top of a run."""
    return AgentState(
        run_dir=run_dir,
        provider_model=provider_model,
        dry_run=dry_run,
        um_per_px=um_per_px,
        calibration_source="unset",
        camera_controls={},
        scene=None,
        fov_history=[],
        scene_decision=None,
        clips=[],
        datasets=[],
        results=[],
        aggregate=None,
        critique=None,
        iteration=0,
        survey_attempts=0,
        started_at=started_at,
        status="starting",
        abort_reason=None,
        report_path=None,
        errors=[],
    )
