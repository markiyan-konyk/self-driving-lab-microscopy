"""MCP server exposing the SCOPIO microscope to Claude Code, on top of the
existing HTTP gateway (via scopio_client). Config: scopio_mcp/.env."""

import io
import os
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage

from scopio_client import Scopio, ScopioError

HERE = Path(__file__).parent      # where the server lives (config only)
RECORDINGS = Path("recordings")   # relative to the CLIENT's working directory


def _load_env():
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()
mcp = MCPServer("scopio")
_scope = None


def scope() -> Scopio:
    global _scope
    if _scope is None:
        key = os.environ.get("SCOPIO_API_KEY", "")
        if not key:
            raise RuntimeError(
                "SCOPIO_API_KEY is not set. Copy scopio_mcp/.env.example to "
                "scopio_mcp/.env and fill in SCOPIO_URL and SCOPIO_API_KEY."
            )
        _scope = Scopio(os.environ.get("SCOPIO_URL", "http://127.0.0.1:8000"), key)
    return _scope


def _frames(count=None, seconds=None):
    """Pull frames off the MJPEG stream, then close it."""
    gen = scope().stream_frames()
    t0 = time.time()
    try:
        for i, jpeg in enumerate(gen, 1):
            yield jpeg
            if count and i >= count:
                return
            if seconds and time.time() - t0 >= seconds:
                return
    finally:
        gen.close()


# ---------------------------------------------------------------- discovery
@mcp.tool()
def describe_instrument() -> dict:
    """The microscope's full capability map: every ROS service, topic and action
    with per-field schemas, plus every driver method on the galvo and
    temperature instruments. Call this first -- it is authoritative and live."""
    s = scope()
    out = s.interfaces()
    out["instrument_methods"] = {}
    for name, ns in (("galvo", s.galvo), ("temperature", s.temperature)):
        try:
            out["instrument_methods"][name] = ns.methods()
        except ScopioError as exc:
            out["instrument_methods"][name] = f"unavailable: {exc}"
    return out


@mcp.tool()
def status() -> dict:
    """Live snapshot: gateway/camera health plus the latest stage position,
    camera state, temperature, AWG status and calibration."""
    s = scope()
    return {"health": s.health(), **s.status()}


# ------------------------------------------------------------------ generic
@mcp.tool()
def call_service(path: str, body: Optional[dict] = None,
                 timeout: float = 10.0) -> Any:
    """Call any ROS service, path relative to /scopio (e.g. 'stage/jog',
    'calibration/set'). Use describe_instrument for paths and field names."""
    return scope().call_service(path, body or {}, timeout=timeout)


@mcp.tool()
def send_goal(action: str, goal: Optional[dict] = None,
              timeout: float = 600.0) -> Any:
    """Run a long-running ROS action to completion: 'camera/autofocus',
    'stage/move_path', 'scan_region'."""
    return scope().send_goal(action, goal or {}, timeout=timeout)


# ------------------------------------------------------------------- motion
@mcp.tool()
def stage_move(dx: int = 0, dy: int = 0, dz: int = 0,
               absolute: bool = False) -> dict:
    """Move the stage, in Sangaboard steps. Relative by default; with
    absolute=True, dx/dy/dz are the target coordinates."""
    st = scope().stage
    return st.move_abs(dx, dy, dz) if absolute else st.jog(dx, dy, dz)


# ------------------------------------------------------------------- camera
@mcp.tool()
def camera_controls(settings: Optional[dict] = None) -> dict:
    """Read the camera controls, or write only the ones you pass. Fields:
    framerate, exposure, analogue_gain, red_gain, blue_gain, contrast,
    saturation, brightness, sharpness."""
    cam = scope().camera
    return cam.set_controls(**settings) if settings else cam.get_controls()


@mcp.tool()
def grab_frame(max_width: int = 800) -> Image:
    """Capture one frame from the live camera and return it as an image to look
    at. Use it to check focus, illumination and what is in the field of view."""
    jpeg = next(_frames(count=1))
    img = PILImage.open(io.BytesIO(jpeg)).convert("RGB")
    img.thumbnail((max_width, max_width))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


@mcp.tool()
def focus_metric() -> dict:
    """Cheap sharpness score for the current view. Higher is sharper; compare
    values across z positions to focus manually."""
    return scope().camera.focus_metric()


@mcp.tool()
def record_clip(seconds: float = 10.0, name: Optional[str] = None) -> dict:
    """Record the camera to numbered JPEG frames for analysis (particle tracking,
    Brownian motion). Saves to recordings/<name>/ inside your working directory,
    so you can read the frames back with your own tools. Returns that path, the
    frame count and the measured fps -- use the MEASURED fps for any timing
    calculation, never the requested one."""
    rel = RECORDINGS / (name or time.strftime("clip_%Y%m%d_%H%M%S"))
    out = Path.cwd() / rel
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    n = 0
    for jpeg in _frames(seconds=seconds):
        (out / f"{n:05d}.jpg").write_bytes(jpeg)
        n += 1
    dt = time.time() - t0
    return {"dir": rel.as_posix(), "abs_dir": str(out), "frames": n,
            "seconds": round(dt, 2), "fps": round(n / dt, 2) if dt else 0.0}


# -------------------------------------------------------------- instruments
@mcp.tool()
def instrument_call(instrument: str, method: str, args: Optional[list] = None,
                    kwargs: Optional[dict] = None) -> Any:
    """Call a driver method on 'galvo' (DG1022Z AWG, optical tweezers) or
    'temperature' (TC LAB). describe_instrument lists every method and its
    signature."""
    ns = {"galvo": scope().galvo, "temperature": scope().temperature}.get(instrument)
    if ns is None:
        raise ValueError("instrument must be 'galvo' or 'temperature'")
    return ns.call(method, *(args or []), **(kwargs or {}))


@mcp.tool()
def galvo_scpi(command: str) -> Any:
    """Send one raw SCPI command to the AWG driving the tweezers. A command
    ending in '?' is sent as a query and returns the instrument's reply."""
    g = scope().galvo
    return g.query(command) if command.strip().endswith("?") else g.write(command)


if __name__ == "__main__":
    mcp.run()
