"""MCP server exposing the SCOPIO microscope to Claude Code, on top of the
existing HTTP gateway (via scopio_client). Config: scopio_mcp/.env.

Tool surface and the discovery contract: see README.md. The short version is
that nothing here hard-codes what the microscope can do -- describe_instrument
asks the live ROS graph and the live driver classes, so a node or a driver
method added on the Pi is usable from here with no change to this file.
"""

import io
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:                                    # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer
except ImportError:                     # mcp 1.x -- same tool/run/Image surface
    from mcp.server.fastmcp import FastMCP as MCPServer, Image

from PIL import Image as PILImage

try:
    from scopio_client import Scopio, ScopioError
except ImportError:                     # the #1 setup failure -- say what to do
    raise SystemExit(
        "scopio_client is not installed for this interpreter "
        f"({sys.executable}).\n"
        "  pip install -e ./scopio_client -r scopio_mcp/requirements.txt\n"
        "and point your .mcp.json 'command' at THAT interpreter.")

HERE = Path(__file__).parent      # where the server lives (config only)
RECORDINGS = Path("recordings")   # relative to the CLIENT's working directory
NS = "/scopio/"                   # the gateway reports full ROS names

# instrument -> (its status topic, what it is). The status topic is read from
# the cached telemetry snapshot, so the index costs the instruments nothing.
INSTRUMENTS = {
    "galvo": ("awg/status",
              "Rigol DG1022Z arbitrary-waveform generator steering the optical "
              "tweezers' galvo mirrors. CH1 = X mirror, CH2 = Y mirror. Mirror "
              "positions are DEFLECTIONS in volts from a calibrated centre, so "
              "0 means centred, not 0 V on the connector (see offsets())."),
    "temperature": ("temperature/status",
                    "Wavelength TC10 LAB sample-temperature controller. Setting "
                    "a setpoint does NOT heat or cool: the TEC output is a "
                    "separate switch. Use the `temperature` tool, which does "
                    "both."),
}

INSTRUCTIONS = """\
SCOPIO is a self-driving-lab microscope: a Raspberry Pi owns the hardware as a
ROS 2 graph, and these tools reach it over an HTTP gateway.

Start with describe_instrument() -- it is a live capability map, not a fixed
menu, so it is authoritative even for hardware added after this server shipped.

What this microscope does NOT have, so you do not go looking:
  * No zoom and no magnification control. The objective is fixed. `dz` on
    stage_move is FOCUS -- it moves the stage along the optical axis; it does
    not make anything bigger. To see a smaller region, you crop the image.
  * No stage readback in micrometres unless a scale is calibrated; positions
    are Sangaboard steps.
  * No galvo position readback from the instrument. `position()` is the last
    commanded value, held by the node, so it does reflect what OTHER clients
    (the web UI, another agent) have done -- but not a hand on the front panel.

And three about driving it:
  * The Pi senses and effects; it never analyses. record_clip writes frames to
    YOUR working directory and you analyse them with your own code.
  * Use the MEASURED fps record_clip returns for any timing, never the
    requested one -- asking for 120 may yield 89.
  * A node reporting connected=false means its hardware is absent or unplugged,
    not that the microscope is broken. Every other node still works.
"""


def _load_env():
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env()
mcp = MCPServer("scopio", instructions=INSTRUCTIONS)
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


def _short(name):
    """'/scopio/stage/jog' -> 'stage/jog' (what every tool here takes)."""
    return name[len(NS):] if name.startswith(NS) else name


def _instrument(name):
    return {"galvo": scope().galvo, "temperature": scope().temperature}.get(name)


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
def describe_instrument(subject: Optional[str] = None) -> dict:
    """What this microscope can do, asked of the live graph. CALL THIS FIRST.

    Two levels, so the whole capability map never lands in context at once (the
    two driver classes alone are ~240 methods):

      describe_instrument()             an index -- every service, topic and
                                        action by name, plus the instruments
      describe_instrument('stage/jog')  the field schema of one service, topic
                                        or action
      describe_instrument('galvo')      every driver method on that instrument,
                                        with signature and docstring
      describe_instrument('galvo.sin')  only the driver methods matching 'sin'

    Instrument methods are read from the CLASS, so they list even while the
    hardware is unplugged. Call them with instrument_call.
    """
    data = scope().interfaces()
    if subject is None:
        telemetry = scope().status().get("telemetry") or {}
        return {
            "services": [_short(n) for n in data["services"]],
            "topics": [_short(n) for n in data["topics"]],
            "actions": [_short(n) for n in data["actions"]],
            "instruments": {
                name: {
                    "what": what,
                    "connected": bool(((telemetry.get(topic) or {}).get("msg")
                                       or {}).get("connected")),
                }
                for name, (topic, what) in INSTRUMENTS.items()
            },
            "next": "describe_instrument('<name>') for a schema, "
                    "'<instrument>' for its driver methods, or "
                    "'<instrument>.<text>' to search them.",
        }

    # Both forms work: the gateway reports '/scopio/stage/jog', every tool here
    # takes 'stage/jog', and an agent will paste either back at this one.
    key = _short(subject.strip()).lstrip("/")
    name, _, query = key.partition(".")
    ns = _instrument(name)
    if ns is not None:
        # `what` travels with the detail, not just the index: an agent that
        # drills straight in here otherwise has to infer the axis-to-channel
        # mapping from the method list, and none of the ~130 docstrings says it.
        what = INSTRUMENTS[name][1]
        try:
            methods = ns.methods()
        except ScopioError as exc:
            return {"instrument": name, "what": what, "error": str(exc)}
        if query:
            methods = [m for m in methods if query.lower() in m["name"].lower()]
        return {"instrument": name, "what": what, "methods": methods,
                "call_with": f"instrument_call('{name}', '<method>', args=[...])"}

    for kind in ("services", "topics", "actions"):
        for full, spec in data[kind].items():
            if _short(full) == key:
                return {"kind": kind[:-1], "name": key, **spec}
    raise ValueError(
        f"no service, topic, action or instrument called {subject!r}. Call "
        "describe_instrument() with no argument for the index of what exists.")


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
    absolute=True, dx/dy/dz are the target coordinates.

    dx/dy pan across the sample; dz is FOCUS -- it moves along the optical axis.
    There is no zoom or magnification control on this microscope, so dz is not
    a substitute for one: it changes what is sharp, never how big it is. To
    focus without hunting by hand, use send_goal('camera/autofocus')."""
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
def camera_mode(mode: Optional[str] = None) -> dict:
    """Read the sensor mode, or switch it. The sensor cannot do both at once:

      'detail'  the most pixels over the FULL field of view -- for looking at
                structure, counting, and measuring.
      'fast'    the highest frame rate the sensor offers, at a CROPPED field of
                view -- for motion: Brownian tracking, flow, anything where the
                time between frames is the measurement.

    Switching reconfigures the sensor, so the video drops for a moment, and the
    frame size changes -- which changes micrometres per pixel. The calibration
    records the width it was measured at, so convert rather than re-measure:
    um_per_px_now = um_per_px * um_per_px_width / width_now.
    """
    cam = scope().camera
    data = cam.set_mode(mode) if mode else cam.get_controls()
    if data.get("error"):
        raise ValueError(data["error"])
    return {"mode": data.get("mode"), "width": data.get("width"),
            "height": data.get("height"), "framerate": data.get("framerate"),
            "modes": data.get("modes") or {}}


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
def white_balance() -> dict:
    """Run a one-shot auto white balance and lock the result. Blocks ~1.5 s
    while the camera converges. Use it when the illumination has changed."""
    return scope().camera.white_balance()


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
    'temperature' (TC LAB). describe_instrument('<instrument>') lists every
    method and its signature."""
    ns = _instrument(instrument)
    if ns is None:
        raise ValueError("instrument must be 'galvo' or 'temperature'")
    return ns.call(method, *(args or []), **(kwargs or {}))


@mcp.tool()
def temperature(celsius: Optional[float] = None,
                enable: Optional[bool] = None) -> dict:
    """Read the sample temperature, or drive it to a setpoint.

    The instrument splits this in two -- a setpoint is only a stored number, and
    the TEC heats or cools nothing until its output is switched on. This tool
    does NOT split it: `temperature(celsius=20)` sets the setpoint AND enables
    the output, because "set the sample to 20 C" means make it be 20 C. Pass
    enable=False to stage a setpoint without driving it, or call
    temperature(enable=False) alone to stop driving and leave the setpoint.

    With no arguments it only reads, from cached telemetry -- free, and safe to
    poll. Reaching a setpoint takes minutes, and the sample lags the sensor:
    watch `in_tolerance`, then dwell before you trust the number.
    """
    tc = scope().temperature
    if celsius is not None:
        tc.setpoint(float(celsius))
    if enable is None:
        enable = celsius is not None        # asking for a temperature means drive it
    if celsius is not None or enable is False:
        tc.output(bool(enable))
    state = tc.status() or {}
    return {"temperature": state.get("temperature"),
            "setpoint": state.get("setpoint"),
            "output_on": state.get("output"),
            "in_tolerance": state.get("in_tolerance"),
            "faults": state.get("faults") or [],
            "note": "status is cached telemetry and lags a write by up to a "
                    "second; poll again rather than re-writing."}


@mcp.tool()
def laser(on: Optional[bool] = None) -> dict:
    """Switch the laser relay, or read it back when called with no argument.

    Its own tool rather than a call_service, so that permitting or refusing
    laser control is a decision you can make separately from everything else.
    A null reading is UNKNOWN, not off: the microscope has never reported a
    relay state, so treat the laser as live until it does.
    """
    state = scope().laser
    if on is None:
        return {"on": state.is_on()}
    result = state.set(bool(on))
    if not result.get("success", True):
        raise RuntimeError(result.get("message") or "relay refused the command")
    return {"on": bool(on), "message": result.get("message", "")}


@mcp.tool()
def galvo_scpi(command: str) -> str:
    """Send one raw SCPI command to the AWG driving the tweezers. A command
    ending in '?' is sent as a query and returns the instrument's reply;
    anything else is a write and returns 'ok'. Raises if the AWG rejects it."""
    g = scope().galvo
    if command.strip().endswith("?"):
        return g.query(command)
    g.write(command)
    return "ok"


if __name__ == "__main__":
    mcp.run()
