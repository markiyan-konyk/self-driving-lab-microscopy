"""MCP server exposing the SCOPIO microscope to Claude Code (or any MCP client),
on top of the existing HTTP gateway (via scopio_client). Config: scopio_mcp/.env.

Design, in two layers:
  * GENERIC tools reach everything the microscope can do, including hardware
    added after this file was written: describe_instrument (the live map),
    call_service, send_goal/goal, read_topic, publish, instrument_call.
  * TASK tools do the common jobs in one call and carry the hardware's rules
    so the agent cannot get them wrong: stage_move, galvo_move, autofocus,
    grab_frame, record_clip, temperature, laser, calibration, ...

INSTRUCTIONS below is the agent's standing brief -- the one place the physics,
units and conventions that no tool schema can express are written down.
"""

import io
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import anyio

try:                                    # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer
except ImportError:                     # mcp 1.x -- same tool/run/Image surface
    from mcp.server.fastmcp import FastMCP as MCPServer, Image
from mcp.types import ToolAnnotations

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

# Instrument nodes are DISCOVERED (every InstrumentCall service). These only
# add a friendlier name and a one-line brief for the ones that exist today; a
# new instrument node shows up under its own name with no change here.
ALIASES = {"galvo": "awg"}                     # agent-facing name -> node name
DISPLAY = {node: name for name, node in ALIASES.items()}
WHAT = {
    "awg": ("Rigol DG1022Z arbitrary-waveform generator steering the optical "
            "tweezers' galvo mirrors. CH1 = X mirror, CH2 = Y mirror. Positions "
            "are DEFLECTIONS in volts from a calibrated centre (offsets()), so 0 "
            "is centred, not 0 V on the connector. Move with galvo_move; read "
            "the AWG rules in the server instructions before scripting it."),
    "temperature": ("Wavelength TC10 LAB sample-temperature controller. Setting "
                    "a setpoint does NOT heat or cool: the TEC output is a "
                    "separate switch. Use the `temperature` tool, which does "
                    "both."),
}

INSTRUCTIONS = """\
SCOPIO is a self-driving-lab microscope: an OpenFlexure stage (Sangaboard), a
Pi camera, optical-tweezers galvo mirrors driven by a Rigol DG1022Z AWG, a
Wavelength TC10 LAB sample temperature controller, and a laser relay. A
Raspberry Pi owns all of it as a ROS 2 graph; these tools reach it over an
HTTP/WebSocket gateway. Anything the microscope can do is reachable from here.

WORKING FAST
 1. describe_instrument() once -- the LIVE map of every service, topic, action
    and instrument (discovered, so it includes hardware added later). Drill in
    only as needed: describe_instrument('stage/jog'), ('galvo'), ('galvo.sin').
 2. status() is cached telemetry: instant, free, safe to call often. Prefer it
    to querying an instrument.
 3. Look with grab_frame: the image comes back to you. roi=[x,y,w,h] zooms
    digitally. stage_move(..., grab=True) moves and looks in ONE call.
 4. Long jobs (scans, paths, autofocus) are ROS actions. send_goal(...,
    wait=False) returns at once; keep working (grab frames), then goal(id) to
    check, wait or cancel.
 5. wait(seconds) to pause for settling. Temperature changes take minutes.

TOOL MAP
  look ......... grab_frame, focus_metric, record_clip, read_topic, status
  stage ........ stage_move, autofocus, send_goal('stage/move_path' | 'scan_region')
  galvo ........ galvo_move (safe DC moves), instrument_call('galvo', ...), galvo_scpi
  temperature .. temperature, instrument_call('temperature', ...)
  laser ........ laser
  camera ....... camera_controls, camera_mode, white_balance
  scale ........ calibration (um/px for the CURRENT sensor mode, steps/um)
  anything ..... call_service, send_goal, goal, publish, read_topic, instrument_call

UNITS AND DIRECTIONS
 * Stage: Sangaboard STEPS, open-loop. Zero is wherever the stage was when its
   node started; it is lost on a restart (no homing, no encoder). dx/dy pan;
   dz is FOCUS (the optical axis). On this rig +x moves the picture LEFT and +y
   moves it UP (a camera-mounting fact). Micrometres only via calibration.
 * Camera: two sensor modes -- 'detail' (full field, most pixels) and 'fast'
   (highest fps, cropped field). um/px depends on the mode; calibration()
   returns it converted for the mode that is running.
 * Galvo: CH1 = X, CH2 = Y, in volts of DEFLECTION from the calibrated centre.
   The connector voltage is deflection + offset (offsets()). position() is the
   last COMMANDED value -- there is no readback.

INSTRUMENT RULES -- the hardware's, not suggestions
 * Tiny input buffers: the AWG and the TC10 lag for seconds when flooded. The
   Pi paces each to at most 50 commands/s; faster calls queue, and a long queue
   times out (10 s per call). Never loop instrument_call rapidly; prefer one
   driver method that does the whole job (e.g. an arbitrary-waveform upload)
   over many small calls. One method can cost several commands (dcinit 4).
 * AWG channel switching: commanding the OTHER channel (X then Y) makes a
   mechanical change inside the AWG that takes time. Move ONE axis, wait, then
   the other. galvo_move does exactly this and skips axes that did not change;
   if you call update() yourself, wait between channels.
 * AWG output range: |connector voltage| <= 2 V is one output range. Crossing
   +/-2 V in either direction flips a mechanical relay -- slow, and it wears
   the relay. Work inside +/-2 V when you can, never plan paths or scans that
   cross it repeatedly, and allow extra settle time after a crossing
   (galvo_move does). For sine/arbitrary outputs the peak is offset + Vpp/2.
 * Laser: laser(on=...). A null reading is UNKNOWN: treat the laser as ON.
   Switch it off when you are done.
 * Temperature: a setpoint heats or cools nothing until the TEC output is on
   (the temperature tool does both). Watch in_tolerance in status, then dwell.

DOES NOT EXIST -- do not go looking
 * No zoom or magnification control (fixed objective; crop with roi).
 * No stage encoder, no galvo position readback.
 * No analysis on the Pi: record_clip writes frames to YOUR working directory;
   analyse them with your own code, using the MEASURED fps it returns.

WHEN THINGS FAIL
 * Errors carry the reason. connected=false on a node means that hardware is
   absent or unplugged; everything else still works.
 * instrument_call('<inst>', 'reconnect') reopens an instrument. An error
   mentioning ConnectHung / "stuck inside the USB stack" needs a person to
   replug the box -- tell the user; do not retry in a loop.
 * Nothing on the Pi enforces ranges on the stage, AWG or TEC: you are the
   limit. When unsure, move in small steps and look.
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
_goals = {}                        # goal id -> scopio_client Goal, until reported done
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


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
    return name[len(NS):] if name.startswith(NS) else name.lstrip("/")


def _instruments(interfaces):
    """{agent-facing name: node name} for every instrument node in the graph."""
    found = {}
    for full, spec in interfaces.get("services", {}).items():
        rel = _short(full)
        if rel.endswith("/call") and str((spec or {}).get("type", "")).endswith("/InstrumentCall"):
            node = rel[:-len("/call")]
            found[DISPLAY.get(node, node)] = node
    return found


def _instrument(name):
    """The driver handle for an instrument, by agent-facing or node name."""
    node = ALIASES.get(name, name)
    return node, scope().instrument(node)


def _safe_name(name, default):
    """Only the final path component: an agent-chosen name must not escape
    recordings/ with '../'."""
    safe = Path(name).name if name else ""
    return default if safe in ("", ".", "..") else safe


def _image(jpeg, max_width, roi):
    """(Image for the agent, info) from one JPEG, optionally cropped to roi."""
    img = PILImage.open(io.BytesIO(jpeg)).convert("RGB")
    frame = list(img.size)
    info = {"frame": frame}
    if roi:
        if len(roi) != 4:
            raise ValueError("roi is [x, y, width, height] in frame pixels")
        x, y, w, h = (int(v) for v in roi)
        x, y = max(0, min(x, frame[0] - 1)), max(0, min(y, frame[1] - 1))
        w, h = max(1, min(w, frame[0] - x)), max(1, min(h, frame[1] - y))
        img = img.crop((x, y, x + w, y + h))
        info["roi"] = [x, y, w, h]
    img.thumbnail((max_width, max_width))
    info["shown"] = list(img.size)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg"), info


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


def _goal_report(handle):
    out = {"goal_id": handle.id, "action": handle.action, "done": handle.done(),
           "feedback": handle.feedback}
    if handle.done():
        _goals.pop(handle.id, None)
        if handle.error is not None:
            out["error"] = handle.error.get("detail") or handle.error.get("code")
        else:
            out.update(status=handle.status, result=handle.result)
    return out


async def _run_goal(handle, timeout):
    """Wait for a goal WITHOUT blocking the MCP event loop. If the client
    cancels this tool call (the user interrupts), or the timeout runs out, the
    goal is CANCELED on the microscope -- it must not keep moving hardware for
    a caller that is gone."""
    deadline = time.monotonic() + timeout
    try:
        while not handle.done():
            if time.monotonic() > deadline:
                handle.cancel()
                raise TimeoutError(f"{handle.action} did not finish within "
                                   f"{timeout:.0f} s; it was canceled")
            await anyio.sleep(0.2)
    except BaseException:
        if not handle.done():
            handle.cancel()
        raise
    return handle.wait(0)          # the result, or raises with the reason


# ================================================================ discovery
@mcp.tool(annotations=_READ_ONLY)
def describe_instrument(subject: Optional[str] = None) -> dict:
    """What this microscope can do, asked of the live graph. CALL THIS FIRST.

    Two levels, so the whole capability map never lands in context at once:

      describe_instrument()             index: every service, topic and action
                                        by name, plus each instrument and
                                        whether it is connected
      describe_instrument('stage/jog')  the field schema of one service, topic
                                        or action (use before call_service /
                                        send_goal / read_topic / publish)
      describe_instrument('galvo')      every driver method of that instrument,
                                        with signature and docstring
      describe_instrument('galvo.sin')  only the driver methods matching 'sin'

    Instrument methods are read from the driver CLASS, so they list even while
    the hardware is unplugged. Call them with instrument_call.
    """
    data = scope().interfaces()
    instruments = _instruments(data)
    if subject is None:
        telemetry = scope().status().get("telemetry") or {}
        return {
            "services": [_short(n) for n in data["services"]],
            "topics": [_short(n) for n in data["topics"]],
            "actions": [_short(n) for n in data["actions"]],
            "instruments": {
                name: {
                    "node": node,
                    "what": WHAT.get(node, f"Instrument node '{node}': its driver "
                                           f"class is exposed over {node}/call."),
                    "connected": bool(((telemetry.get(f"{node}/status") or {}).get("msg")
                                       or {}).get("connected")),
                }
                for name, node in instruments.items()
            },
            "next": "describe_instrument('<name>') for a schema, '<instrument>' "
                    "for its driver methods, or '<instrument>.<text>' to search them.",
        }

    # Both forms work: the gateway reports '/scopio/stage/jog', every tool here
    # takes 'stage/jog', and an agent will paste either back at this one.
    key = _short(subject.strip())
    name, _, query = key.partition(".")
    if name in instruments or name in instruments.values():
        node, handle = _instrument(name)
        what = WHAT.get(node, f"Instrument node '{node}'.")
        try:
            methods = handle.methods()
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


@mcp.tool(annotations=_READ_ONLY)
def status(topic: Optional[str] = None) -> dict:
    """Live snapshot, from cached telemetry (instant; no instrument traffic).

    No argument: gateway/camera health plus the latest message of every state
    topic -- stage/position, camera/state, awg/status, temperature/status,
    relay/state, calibration, and any <node>/status of newer nodes. A null
    entry means that node has not published (absent or starting).
    topic='temperature/status': just that one entry.
    """
    s = scope()
    snapshot = s.status()
    if topic:
        key = _short(topic)
        return {"topic": key, "entry": (snapshot.get("telemetry") or {}).get(key)}
    return {"health": s.health(), **snapshot}


# ================================================================== generic
@mcp.tool()
def call_service(path: str, body: Optional[dict] = None,
                 timeout: float = 10.0) -> Any:
    """Call any ROS service, path relative to /scopio (e.g. 'stage/jog',
    'calibration/set', 'relay/set'). body maps to the request fields -- see
    describe_instrument('<path>'). A float field you leave out means "leave it
    unchanged". Returns the response fields as the node sent them."""
    return scope().call_service(path, body or {}, timeout=timeout)


@mcp.tool()
async def send_goal(action: str, goal: Optional[dict] = None, wait: bool = True,
                    timeout: float = 600.0) -> Any:
    """Run a long ROS action: 'camera/autofocus', 'stage/move_path',
    'scan_region' (goal fields: describe_instrument('<action>')).

    wait=True (default): returns {"status", "result"} when it finishes.
    wait=False: returns {"goal_id"} at once, so you can keep working (grab
    frames while a scan runs); then goal(goal_id) to check, wait or cancel.
    A timeout, or interrupting this call, CANCELS the goal on the microscope.
    """
    handle = await anyio.to_thread.run_sync(
        lambda: scope().start_goal(action, goal or {}))
    if not wait:
        _goals[handle.id] = handle
        return {"goal_id": handle.id, "action": action, "accepted": True,
                "next": "goal(goal_id) to check progress, wait, or cancel"}
    return await _run_goal(handle, timeout)


@mcp.tool()
async def goal(goal_id: str, cancel: bool = False, wait_s: float = 0.0) -> dict:
    """Check on a goal started with send_goal(wait=False).

    Returns done, the latest feedback, and -- once finished -- status and result
    (or the error). cancel=True asks the microscope to stop it. wait_s>0 waits
    up to that long for it to finish before answering.
    """
    handle = _goals.get(goal_id)
    if handle is None:
        raise ValueError(f"no running goal {goal_id!r} (finished goals are "
                         f"reported once). Running: {sorted(_goals)}")
    if cancel:
        handle.cancel()
    deadline = time.monotonic() + max(0.0, wait_s)
    while not handle.done() and time.monotonic() < deadline:
        await anyio.sleep(0.2)
    return _goal_report(handle)


@mcp.tool(annotations=_READ_ONLY)
def read_topic(topic: str, count: int = 1, timeout_s: float = 5.0) -> dict:
    """Read the next `count` messages (max 50) from any non-video topic, live
    over the WebSocket -- for topics that are not in status(), or when you need
    several consecutive samples. topic is relative to /scopio."""
    key, count = _short(topic), max(1, min(50, int(count)))
    messages, done = [], threading.Event()

    def on_message(msg, _envelope):
        messages.append(msg)
        if len(messages) >= count:
            done.set()

    sub = scope().subscribe(key, on_message)
    try:
        done.wait(timeout_s)
    finally:
        sub.unsubscribe()
    return {"topic": key, "messages": messages[:count],
            "complete": len(messages) >= count}


@mcp.tool()
def publish(topic: str, msg: dict, msg_type: Optional[str] = None) -> dict:
    """Publish one message on a topic (relative to /scopio). msg_type
    ('pkg/msg/Type') is needed only when nothing publishes that topic yet.
    Rarely needed: commands normally go through services."""
    scope().publish(_short(topic), msg, msg_type)
    return {"published": _short(topic)}


# =================================================================== motion
@mcp.tool(structured_output=False)
def stage_move(dx: int = 0, dy: int = 0, dz: int = 0, absolute: bool = False,
               grab: bool = False, max_width: int = 800):
    """Move the stage, in Sangaboard steps; returns once it has stopped, with
    the new position. Relative by default; absolute=True makes dx/dy/dz the
    target coordinates.

    dx/dy pan across the sample (+x moves the picture LEFT, +y moves it UP on
    this rig); dz is FOCUS along the optical axis -- never a zoom. grab=True
    also returns a fresh frame taken after the move (one call instead of two).
    Raises if the stage is unavailable.
    """
    st = scope().stage
    moved = st.move_abs(dx, dy, dz) if absolute else st.jog(dx, dy, dz)
    if not grab:
        return moved
    image, info = _image(scope().camera.snapshot(discard=1), max_width, None)
    return [moved, info, image]


@mcp.tool()
async def autofocus(z_range: int = 2000, steps: int = 15,
                    settle_s: float = 0.2) -> Any:
    """Focus automatically: sweep Z over +/-z_range steps in `steps` positions,
    score sharpness at each, and park at the sharpest. Returns best_z and the
    score. Takes roughly steps x (move + settle_s + one frame). Interrupting
    this call cancels the sweep."""
    handle = await anyio.to_thread.run_sync(lambda: scope().start_goal(
        "camera/autofocus",
        {"z_range": int(z_range), "steps": int(steps), "settle_s": float(settle_s)}))
    return await _run_goal(handle, 600.0)


@mcp.tool()
def galvo_move(x: Optional[float] = None, y: Optional[float] = None,
               axis_settle_s: Optional[float] = None,
               range_settle_s: Optional[float] = None) -> dict:
    """Point the tweezers: move the galvo mirrors by DC offset, safely. x = CH1,
    y = CH2, in volts of deflection from the calibrated centre; omit an axis to
    leave it where it is.

    Follows the AWG's rules for you: one axis at a time, only axes that change
    (no needless channel switch), waiting after each (axis_settle_s, default
    0.2 s) -- and longer (range_settle_s, default 0.5 s) when a move crosses
    +/-2 V at the connector, where a relay flips. Returns once settled:
    {"x", "y", "moved", "range_switched", "waited_s"}.
    """
    return scope().galvo.move_xy(x, y, axis_settle_s=axis_settle_s,
                                 range_settle_s=range_settle_s)


# =================================================================== camera
@mcp.tool(structured_output=False)
def grab_frame(max_width: int = 800, roi: Optional[list] = None, discard: int = 0,
               save_as: Optional[str] = None):
    """Capture one fresh frame and return it as an image you can see, plus its
    geometry. Use it to check focus, illumination and what is in the field.

    roi=[x, y, width, height] in FRAME pixels crops before downscaling -- the
    digital zoom (the objective is fixed). discard=N skips N frames first (to
    let the image settle after a move). save_as='name' also writes the full
    frame, uncropped, to recordings/frames/<name>.jpg in your working directory.
    """
    jpeg = scope().camera.snapshot(discard=max(0, int(discard)))
    image, info = _image(jpeg, max_width, roi)
    if save_as:
        out = RECORDINGS / "frames" / (_safe_name(save_as, "frame").removesuffix(".jpg") + ".jpg")
        (Path.cwd() / out).parent.mkdir(parents=True, exist_ok=True)
        (Path.cwd() / out).write_bytes(jpeg)
        info["saved"] = out.as_posix()
    return [info, image]


@mcp.tool()
def camera_controls(settings: Optional[dict] = None) -> dict:
    """Read the camera settings, or write only the ones you pass (the rest are
    untouched). Fields: framerate, exposure (us), analogue_gain, red_gain,
    blue_gain, contrast, saturation, brightness, sharpness. A frame cannot be
    shorter than its exposure: a long exposure lowers the frame rate."""
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

    Switching reconfigures the sensor, so the video drops for a moment. The
    frame size changes; calibration() gives the um/px for the new mode.
    """
    cam = scope().camera
    try:
        data = cam.set_mode(mode) if mode else cam.get_controls()
    except ScopioError as exc:
        if exc.status == 400:               # a refused mode, not a dead link
            raise ValueError(str(exc)) from exc
        raise
    if data.get("error"):
        raise ValueError(data["error"])
    return {"mode": data.get("mode"), "width": data.get("width"),
            "height": data.get("height"), "framerate": data.get("framerate"),
            "modes": data.get("modes") or {}}


@mcp.tool()
def white_balance() -> dict:
    """Run a one-shot auto white balance and lock the result. Blocks ~1.5 s
    while the camera converges. Use it when the illumination has changed."""
    return scope().camera.white_balance()


@mcp.tool(annotations=_READ_ONLY)
def focus_metric() -> dict:
    """Cheap sharpness score for the current view (higher is sharper). Only
    comparable between frames of the same scene -- use it across dz steps to
    focus by hand; autofocus does the whole sweep for you."""
    return scope().camera.focus_metric()


@mcp.tool()
def record_clip(seconds: float = 10.0, name: Optional[str] = None) -> dict:
    """Record the camera to numbered JPEG frames for analysis (particle tracking,
    Brownian motion). Saves to recordings/<name>/ inside your working directory,
    so you can read the frames back with your own tools. Returns that path, the
    frame count and the MEASURED fps -- use it for any timing calculation, never
    the requested frame rate. At most 600 s per call."""
    seconds = max(0.1, min(600.0, float(seconds)))
    rel = RECORDINGS / _safe_name(name, time.strftime("clip_%Y%m%d_%H%M%S"))
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


@mcp.tool()
def calibration(um_per_px: Optional[float] = None,
                steps_per_um_x: Optional[float] = None,
                steps_per_um_y: Optional[float] = None,
                steps_per_um_z: Optional[float] = None) -> dict:
    """Read or set the spatial calibration.

    No arguments: um_per_px_now -- micrometres per pixel of the frame the
    camera is producing NOW (converted from the mode it was measured in) --
    plus the stored calibration and the running frame size.
    um_per_px=...: store a new image scale MEASURED ON THE CURRENT FRAME (e.g.
    known length in um / its length in grab_frame pixels); the current mode's
    geometry is recorded with it so it converts to the other mode.
    steps_per_um_x/y/z=...: store the stage scale per axis.
    """
    s = scope()
    changes = {k: v for k, v in (("steps_per_um_x", steps_per_um_x),
                                 ("steps_per_um_y", steps_per_um_y),
                                 ("steps_per_um_z", steps_per_um_z)) if v is not None}
    controls = s.camera.get_controls()
    if um_per_px is not None:
        changes.update(um_per_px=float(um_per_px),
                       um_per_px_width=int(controls.get("width") or 0),
                       um_per_px_window=int(controls.get("window") or 0))
    if changes:
        s.calibration.set(**changes)
        time.sleep(0.3)                 # the latched topic reaches the gateway
    return {"um_per_px_now": s.calibration.um_per_px(controls.get("width"),
                                                     controls.get("window")),
            "mode": controls.get("mode"),
            "frame": [controls.get("width"), controls.get("height")],
            "stored": s.calibration.get()}


# ============================================================== instruments
@mcp.tool()
def instrument_call(instrument: str, method: str, args: Optional[list] = None,
                    kwargs: Optional[dict] = None) -> Any:
    """Call any driver method of an instrument: 'galvo' (DG1022Z AWG, the
    tweezers), 'temperature' (TC10 LAB), or any instrument node added later
    (see describe_instrument()). describe_instrument('<instrument>') lists every
    method with its signature. Meta-methods on every instrument: list_methods,
    connected, reconnect. Mind the AWG rules in the server instructions."""
    node, handle = _instrument(instrument)
    try:
        return handle.call(method, *(args or []), **(kwargs or {}))
    except ScopioError as exc:
        if exc.status == 404:
            names = sorted(_instruments(scope().interfaces()))
            raise ValueError(f"no instrument {instrument!r}; available: {names}") from exc
        raise


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
            "connected": state.get("connected"),
            "note": "status is cached telemetry and lags a write by up to a "
                    "second; poll again rather than re-writing."}


@mcp.tool()
def laser(on: Optional[bool] = None) -> dict:
    """Switch the laser relay, or read it back when called with no argument.

    Its own tool rather than a call_service, so that permitting or refusing
    laser control is a decision you can make separately from everything else.
    A null reading is UNKNOWN, not off: the microscope has never reported a
    relay state, so treat the laser as live until it does. Raises if the relay
    refused -- a failed "off" never looks like a successful one.
    """
    relay = scope().laser
    if on is None:
        return {"on": relay.is_on()}
    result = relay.set(bool(on))
    return {"on": bool(on), "message": result.get("message", "")}


@mcp.tool()
def galvo_scpi(command: str) -> str:
    """Send one raw SCPI command to the AWG driving the tweezers. A command
    ending in '?' is sent as a query and returns the instrument's reply;
    anything else is a write and returns 'ok'. Raises if the AWG rejects it.
    Prefer galvo_move / instrument_call('galvo', ...); the AWG rules apply."""
    g = scope().galvo
    if command.strip().endswith("?"):
        return g.query(command)
    g.write(command)
    return "ok"


# ===================================================================== time
@mcp.tool(annotations=_READ_ONLY)
async def wait(seconds: float) -> dict:
    """Pause for `seconds` (max 3600) -- for settling after moves, or while a
    temperature approaches its setpoint. Interrupting the call ends the wait."""
    seconds = max(0.0, min(3600.0, float(seconds)))
    await anyio.sleep(seconds)
    return {"waited_s": seconds}


if __name__ == "__main__":
    mcp.run()
