"""Instrument control -- thin, logged wrappers over the scope's stage / camera /
calibration surface. Every call is recorded to the notebook. All movement is
clamped so a bad LLM decision can never drive the stage wildly.

These are plain functions taking the run Context (not LangChain @tool objects):
the deterministic nodes call them directly. The scope may be a real
scopio_client.Scopio or the dry-run MockScope -- identical method surface.
"""

from scopio_client import ScopioError

from ..context import Context


def _try(ctx: Context, name: str, fn, args: dict):
    """Run a scope call, log it, and normalise ScopioError into a dict."""
    try:
        res = fn()
        ctx.nb.tool(name, args, _summ(res))
        return {"ok": True, "result": res}
    except ScopioError as e:
        ctx.nb.error(f"{name} failed: {e}", tool=name, args=args)
        return {"ok": False, "error": str(e)}


def _summ(res):
    if isinstance(res, dict):
        keys = ", ".join(list(res)[:6])
        return "{" + keys + ("…}" if len(res) > 6 else "}")
    return str(res)[:80]


# --------------------------------------------------------------------- health
def get_health(ctx: Context):
    return _try(ctx, "get_health", ctx.scope.health, {})


def get_status_summary(ctx: Context):
    return _try(ctx, "get_status", ctx.scope.status, {})


# ----------------------------------------------------------------- calibration
def get_calibration(ctx: Context):
    try:
        c = ctx.scope.calibration.get()
    except ScopioError as e:
        ctx.nb.error(f"get_calibration failed: {e}")
        return {"has_um_per_px": False, "um_per_px": None, "error": str(e)}
    if not c or not c.get("has_um_per_px"):
        ctx.nb.tool("get_calibration", {}, "unset")
        return {"has_um_per_px": False, "um_per_px": None}
    val = c.get("um_per_px")
    ctx.nb.tool("get_calibration", {}, f"{val:g} um/px")
    return {"has_um_per_px": True, "um_per_px": float(val)}


def set_calibration(ctx: Context, um_per_px: float):
    return _try(ctx, "set_calibration",
                lambda: ctx.scope.calibration.set(um_per_px=float(um_per_px)),
                {"um_per_px": um_per_px})


# ---------------------------------------------------------------------- stage
def get_position(ctx: Context):
    try:
        p = ctx.scope.stage.position()
    except ScopioError as e:
        ctx.nb.error(f"get_position failed: {e}")
        return None
    ctx.nb.tool("get_position", {}, _summ(p))
    return p


def jog_stage(ctx: Context, dx=0, dy=0, dz=0):
    """Relative stage move (Sangaboard steps), clamped to +/- max_jog_steps."""
    lim = ctx.cfg.max_jog_steps
    dx, dy, dz = (max(-lim, min(lim, int(v))) for v in (dx, dy, dz))
    return _try(ctx, "jog_stage",
                lambda: ctx.scope.stage.jog(dx=dx, dy=dy, dz=dz),
                {"dx": dx, "dy": dy, "dz": dz})


def move_stage_abs(ctx: Context, x, y, z):
    return _try(ctx, "move_stage_abs",
                lambda: ctx.scope.stage.move_abs(int(x), int(y), int(z)),
                {"x": x, "y": y, "z": z})


# --------------------------------------------------------------------- camera
def autofocus(ctx: Context):
    return _try(ctx, "autofocus",
                lambda: ctx.scope.camera.autofocus(z_range=2000, steps=15,
                                                   settle_s=0.2),
                {})


def white_balance(ctx: Context):
    return _try(ctx, "white_balance", ctx.scope.camera.white_balance, {})


def get_camera_controls(ctx: Context):
    try:
        c = ctx.scope.camera.get_controls()
    except ScopioError as e:
        ctx.nb.error(f"get_camera_controls failed: {e}")
        return {}
    ctx.nb.tool("get_camera_controls", {}, _summ(c))
    return c


# camera control clamps (conservative; mirror the UI's limits)
_CAM_CLAMPS = {
    "framerate": (1.0, 120.0),
    "exposure": (100.0, 200000.0),
    "analogue_gain": (1.0, 16.0),
    "red_gain": (0.1, 8.0),
    "blue_gain": (0.1, 8.0),
    "contrast": (0.0, 4.0),
    "saturation": (0.0, 4.0),
    "brightness": (-1.0, 1.0),
    "sharpness": (0.0, 4.0),
}


def set_camera(ctx: Context, **updates):
    clean = {}
    for k, v in updates.items():
        if v is None or k not in _CAM_CLAMPS:
            continue
        lo, hi = _CAM_CLAMPS[k]
        clean[k] = float(max(lo, min(hi, float(v))))
    if not clean:
        return {"ok": False, "error": "no valid camera fields"}
    return _try(ctx, "set_camera",
                lambda: ctx.scope.camera.set_controls(**clean), clean)
