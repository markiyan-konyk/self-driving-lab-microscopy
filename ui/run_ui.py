#!/usr/bin/env python3
"""SCOPIO Web UI - a standalone API CLIENT application.

This program owns NO hardware and speaks NO ROS. It is the reference UI for
the SCOPIO microscope and talks to the Pi's API gateway over plain HTTP/
WebSocket through the scopio_client SDK:

  subscribes (WS):  camera/state, stage/position, awg/status, beads,
                    calibration, temperature/status
  video (MJPEG):    /api/v1/stream.mjpg  -> live view + client-side recording
  services (HTTP):  stage/jog, calibration/set, awg/write, temperature/call,
                    camera controls
  action (WS):      camera/autofocus (runs on the backend)

Because it is a plain HTTP client it runs on ANY machine that can reach the
Pi -- no Docker, no WSL2, no DDS, no firewall rules. Several people can run
their own UI against the same microscope at once.

Recording happens HERE, client-side: the ingested JPEG stream is written to
MP4 in THIS app's own ./recordings folder, so footage lives with whoever runs
the UI, and the Pi takes no recording/disk load.

Run:
    pip install -r requirements.txt        # includes -e ../scopio_client
    set SCOPIO_URL=http://<pi-ip>:8000
    set SCOPIO_API_KEY=<key from ros2_ws/scripts/generate_api_key.py>
    python run_ui.py                       # serves http://0.0.0.0:8080

Env: SCOPIO_URL, SCOPIO_API_KEY (required); SCOPIO_UI_PORT (8080),
     SCOPIO_UI_PASSWORD ("password").
"""

import os
import re
import json
import math
import time
import secrets
import logging
import threading
import subprocess
from functools import wraps

from flask import (
    Flask, Response, jsonify, render_template_string, request, session,
    redirect, url_for, send_from_directory, abort,
)

from scopio_client import Scopio, ScopioError
from galvo_geometry import GalvoClient

# ========== Config ==========
HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(HERE, "frontend")
RECORDINGS_DIR = os.path.join(HERE, "recordings")
PORT = int(os.environ.get("SCOPIO_UI_PORT", 8080))
PASSWORD = os.environ.get("SCOPIO_UI_PASSWORD", "password")
SCOPIO_URL = os.environ.get("SCOPIO_URL", "http://127.0.0.1:8000")
SCOPIO_API_KEY = os.environ.get("SCOPIO_API_KEY", "")

MIN_FPS, MAX_FPS = 1, 120

log = logging.getLogger("scopio_ui")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")


# Screen-direction -> stage displacement, mirrors the backend's convention
# (camera mounted 90 deg to the stage: screen up/down = stage X, left/right = Y).
def dir_delta(direction, steps):
    return {
        "up":        (steps["x"], 0, 0),
        "down":      (-steps["x"], 0, 0),
        "left":      (0, -steps["y"], 0),
        "right":     (0, steps["y"], 0),
        "page_up":   (0, 0, steps["z"]),
        "page_down": (0, 0, -steps["z"]),
    }.get(direction)


class State:
    """Live microscope state, fed by SDK subscriptions + the MJPEG ingest."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = None            # newest JPEG frame (live view + recording)
        self.stream_fps = 0.0       # measured fps of the ingested stream
        self.camera = None          # camera/state message dict
        self.stage = None           # stage/position message dict
        self.awg = None             # awg/status message dict
        self.temp = None            # temperature/status message dict
        self.beads = None           # beads message dict
        self.calibration = None     # calibration message dict (latched)
        self.connected = False      # gateway subscriptions established


state = State()
scope = None      # set in main()
galvo = GalvoClient()
steps = {"x": 40, "y": 40, "z": 40}     # client-side step sizes
record_duration = 600                    # seconds, or None for infinite
_wb_running = False
_af_running = False

# ---- client-side recording state ----
_rec = {"active": False, "thread": None, "stop": threading.Event(),
        "filename": None, "started_at": None, "duration": None}
_probe_cache = {}


def _read(name):
    with open(os.path.join(FRONTEND_DIR, name), encoding="utf-8") as f:
        return f.read()


# ---- background workers ----
def _subscribe_loop():
    """Establish the WS subscriptions; retry until the gateway is reachable
    (so the UI comes up fine even if the Pi boots later)."""
    def store(attr):
        def cb(msg, _envelope):
            with state.lock:
                setattr(state, attr, msg)
        return cb

    while True:
        try:
            scope.subscribe("camera/state", store("camera"), rate_hz=4)
            scope.subscribe("stage/position", store("stage"), rate_hz=10)
            scope.subscribe("awg/status", store("awg"), rate_hz=2)
            scope.subscribe("beads", store("beads"), rate_hz=5)
            scope.subscribe("calibration", store("calibration"))
            state.connected = True
            log.info("subscribed to microscope telemetry")
            break
        except ScopioError as e:
            log.warning(f"cannot subscribe yet ({e}); retrying in 3 s")
            time.sleep(3)

    # Temperature is OPTIONAL and retried separately: a microscope without the
    # controller (or an older backend without the node) must not cost us the
    # camera/stage telemetry above, which a single failing subscribe would.
    while True:
        try:
            scope.subscribe("temperature/status", store("temp"), rate_hz=1)
            log.info("subscribed to temperature telemetry")
            return
        except ScopioError as e:
            log.info(f"no temperature node yet ({e}); retrying in 15 s")
            time.sleep(15)


def _frame_ingest_loop():
    """Pull the live MJPEG stream through the gateway into state.jpeg -- feeds
    /video_feed, recording and the focus display. Reconnects forever."""
    last_t = time.time()
    while True:
        try:
            for frame in scope.stream_frames():
                with state.lock:
                    state.jpeg = frame
                now = time.time()
                dt = now - last_t
                last_t = now
                if dt > 0:
                    state.stream_fps = 0.85 * state.stream_fps + 0.15 * (1.0 / dt)
        except ScopioError as e:
            state.stream_fps = 0.0
            log.warning(f"camera stream unavailable ({e}); retrying in 2 s")
            time.sleep(2)


# ========== Flask app ==========
app = Flask(__name__)
app.secret_key = os.environ.get("SCOPIO_UI_SESSION_SECRET") or secrets.token_hex(16)


# ---- auth ----
def login_required(view):
    @wraps(view)
    def wrapped(*a, **k):
        if session.get("authed"):
            return view(*a, **k)
        return redirect(url_for("login"))
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if PASSWORD and secrets.compare_digest(request.form.get("password", ""), PASSWORD):
            session.clear()
            session["authed"] = True
            return redirect(url_for("index"))
        error = "Incorrect password"
    html = _read("login.html").replace("__LOGO__", _read("logo.svg"))
    return render_template_string(html, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    cam = _cam_dict()
    document = (_read("index.html")
                .replace("__STYLE__", _read("style.css"))
                .replace("__SCRIPT__", _read("app.js"))
                .replace("__LOGO__", _read("logo.svg")))
    return render_template_string(document, steps=steps, cam=cam,
                                  record_duration=record_duration,
                                  min_fps=MIN_FPS, max_fps=MAX_FPS)


# ---- camera state helpers ----
def _cam_dict():
    """Current camera controls as a plain dict for the template / API."""
    with state.lock:
        cs = state.camera
    if cs is None:
        return {"red_gain": 2.4, "green_gain": 1.0, "blue_gain": 2.5, "framerate": 30,
                "exposure": 20000, "analogue_gain": 1.0, "colour_gain": 1.0,
                "contrast": 1.0, "saturation": 1.0, "brightness": 0.0, "sharpness": 1.0}
    return {"red_gain": cs["red_gain"], "green_gain": cs["green_gain"],
            "blue_gain": cs["blue_gain"], "framerate": cs["target_fps"],
            "exposure": cs["exposure_us"], "analogue_gain": cs["analogue_gain"],
            "colour_gain": cs["colour_gain"], "contrast": cs["contrast"],
            "saturation": cs["saturation"], "brightness": cs["brightness"],
            "sharpness": cs["sharpness"]}


@app.route("/video_feed")
@login_required
def video_feed():
    def gen():
        while True:
            with state.lock:
                jpeg = state.jpeg
            if jpeg is None:
                time.sleep(0.05)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
            time.sleep(1 / 15)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/status")
@login_required
def status():
    with state.lock:
        st = state.stage
    return jsonify({"controller_connected": bool(st and st["connected"]),
                    "steps": steps})


@app.route("/telemetry")
@login_required
def telemetry():
    with state.lock:
        st, cs, awg = state.stage, state.camera, state.awg
    pos = {"x": st["x"], "y": st["y"], "z": st["z"]} if st else {"x": 0, "y": 0, "z": 0}
    stage_um = ({"x": st["x_um"], "y": st["y_um"], "z": st["z_um"]}
                if st else {"x": 0, "y": 0, "z": 0})
    laser = galvo.state()
    laser["connected"] = bool(awg and awg["connected"])
    laser["global_um"] = {k: round(v, 1) for k, v in galvo.global_um(stage_um).items()}
    fps = round(state.stream_fps, 1) or (cs["measured_fps"] if cs else 0.0)
    return jsonify({
        "position": pos,
        "fps": fps,
        "target_fps": cs["target_fps"] if cs else 0.0,
        "controller_connected": bool(st and st["connected"]),
        "laser": laser,
    })


# ---- stage ----
@app.route("/move/<direction>", methods=["GET", "POST"])
@login_required
def move(direction):
    delta = dir_delta(direction, steps)
    if delta is None:
        return "Unknown direction", 404
    try:
        res = scope.stage.jog(*delta)
        if not res.get("success"):
            return res.get("message") or "jog failed", 503
    except ScopioError as e:
        return str(e), 503
    return "OK", 200


@app.route("/adjust/<axis>/<op>", methods=["GET", "POST"])
@login_required
def adjust(axis, op):
    if axis not in steps:
        return "Unknown axis", 404
    steps[axis] = (steps[axis] + 5) if op == "inc" else max(1, steps[axis] - 5)
    return str(steps[axis]), 200


@app.route("/set_step/<axis>/<int:value>", methods=["GET", "POST"])
@login_required
def set_step(axis, value):
    if axis not in steps:
        return "Unknown axis", 404
    steps[axis] = max(1, int(value))
    return str(steps[axis]), 200


# ---- camera (curated gateway endpoints -> the Pi camera server) ----
@app.route("/get_camera_controls")
@login_required
def get_camera_controls():
    try:
        return jsonify(scope.camera.get_controls())
    except ScopioError:
        return jsonify(_cam_dict())     # fall back to cached ROS state


@app.route("/set_camera_controls", methods=["POST"])
@login_required
def set_camera_controls():
    d = request.get_json() or {}
    try:
        scope.camera.set_controls(**d)
    except ScopioError as e:
        return str(e), 503
    return "OK"


@app.route("/set_framerate", methods=["POST"])
@login_required
def set_framerate():
    d = request.get_json() or {}
    fps = float(d.get("fps", 30))
    try:
        res = scope.camera.set_controls(framerate=fps)
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"framerate": res.get("framerate", fps),
                    "exposure": res.get("exposure", 0),
                    "analogue_gain": res.get("analogue_gain", 1.0)})


@app.route("/white_balance", methods=["POST"])
@login_required
def white_balance():
    global _wb_running
    if _wb_running:
        return jsonify({"error": "Calibration already in progress"}), 409
    _wb_running = True

    def run():
        global _wb_running
        try:
            res = scope.camera.white_balance()
            log.info(f"white balance: {res}")
        except ScopioError as e:
            log.warning(f"white balance failed: {e}")
        finally:
            _wb_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"message": "White balance started"})


@app.route("/calibration_status")
@login_required
def calibration_status():
    return jsonify({"running": _wb_running or _af_running})


@app.route("/autofocus", methods=["POST"])
@login_required
def autofocus():
    global _af_running
    if _af_running or _wb_running:
        return jsonify({"error": "Calibration already in progress"}), 409
    _af_running = True

    def run():
        global _af_running
        try:
            # Backend action: the camera node sweeps Z with the stage and
            # measures sharpness on its own frames -- works for ANY client.
            res = scope.camera.autofocus(z_range=2000, steps=15, settle_s=0.2)
            r = res.get("result") or {}
            log.info(f"autofocus {res.get('status')}: {r.get('message')} "
                     f"(best_z={r.get('best_z')})")
        except ScopioError as e:
            log.warning(f"autofocus failed: {e}")
        finally:
            _af_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"message": "Autofocus started"})


# ---- calibration ----
@app.route("/get_calibration")
@login_required
def get_calibration():
    with state.lock:
        c = state.calibration
    if c is None or not c.get("has_um_per_px"):
        return jsonify({"um_per_px": None})
    return jsonify({"um_per_px": c["um_per_px"]})


@app.route("/set_calibration", methods=["POST"])
@login_required
def set_calibration():
    d = request.get_json() or {}
    try:
        px = float(d["pixels"]); um = float(d["micrometres"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Need 'pixels' and 'micrometres'"}), 400
    if px <= 0 or um <= 0:
        return jsonify({"error": "Values must be positive"}), 400
    try:
        scope.calibration.set(um_per_px=um / px)
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"um_per_px": um / px, "ref_pixels": px, "ref_micrometres": um})


# ---- galvo (compose SCPI client-side, send via awg/write) ----
@app.route("/galvo/move/<direction>", methods=["POST"])
@login_required
def galvo_move(direction):
    try:
        scope.galvo.write_all(galvo.jog_cmds(direction))
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify(_laser_state())


@app.route("/galvo/zero", methods=["POST"])
@login_required
def galvo_zero():
    galvo.set_home()
    return jsonify(_laser_state())


@app.route("/galvo/set_jog", methods=["POST"])
@login_required
def galvo_set_jog():
    d = request.get_json() or {}
    try:
        galvo.set_jog_volts(float(d.get("volts", 0.05)))
    except (TypeError, ValueError):
        return jsonify({"error": "bad volts"}), 400
    return jsonify({"jog_volts": galvo.jog_volts})


def _laser_state():
    with state.lock:
        st, awg = state.stage, state.awg
    stage_um = ({"x": st["x_um"], "y": st["y_um"], "z": st["z_um"]}
                if st else {"x": 0, "y": 0, "z": 0})
    s = galvo.state()
    s["connected"] = bool(awg and awg["connected"])
    s["global_um"] = {k: round(v, 1) for k, v in galvo.global_um(stage_um).items()}
    return s


# ---- temperature (sample environment) ----
# The node exposes the WHOLE driver class (setpoint, PID, IntelliTune, limits,
# ...); this UI deliberately uses only the few calls an operator needs at the
# microscope. A dedicated temperature app can use the rest -- ask the
# instrument what it has with scope.temperature.methods().
UNIT_NAMES = {0: "°C", 1: "K", 2: "°F", 3: "raw"}

# Ramping is CLIENT policy: the controller has no ramp command, so we walk its
# setpoint. Same split as the galvo (node = hardware, app = meaning). A ramp
# lives in this process only -- restart the UI and the setpoint simply stays
# where the last step left it.
RAMP_TICK_S = 2.0
_ramp = {"active": False, "thread": None, "stop": threading.Event(),
         "target": None, "rate": 0.0, "commanded": None, "error": ""}


def _ramp_cancel():
    """Stop any ramp in flight and wait for its thread to notice."""
    _ramp["stop"].set()
    t = _ramp["thread"]
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=RAMP_TICK_S + 2.0)
    _ramp["active"] = False


def _ramp_loop(stop, start, target, rate):
    """Walk the setpoint from `start` to `target` at `rate` degrees/minute.

    Every step is clamped to the target, so the ramp can never overshoot; the
    instrument's own PID does the actual following. `stop` is passed in (not
    read from _ramp) so a cancelled ramp can never be revived by, or interfere
    with, the ramp that replaced it.
    """
    per_tick = abs(rate) * (RAMP_TICK_S / 60.0)
    commanded = start
    try:
        while not stop.is_set():
            remaining = target - commanded
            commanded += math.copysign(min(per_tick, abs(remaining)), remaining)
            scope.temperature.setpoint(round(commanded, 3))
            _ramp["commanded"] = commanded
            if abs(target - commanded) < 1e-6:
                log.info(f"temperature ramp reached {target:g}")
                break
            if stop.wait(RAMP_TICK_S):
                break
    except ScopioError as e:
        _ramp["error"] = str(e)
        log.warning(f"temperature ramp aborted: {e}")
    finally:
        if _ramp["stop"] is stop:      # not superseded by a newer ramp
            _ramp["active"] = False


def _r(value, digits=3):
    """Round, but keep null null -- the gateway sends NaN readings as null."""
    return None if value is None else round(value, digits)


def _temp_state():
    with state.lock:
        t = state.temp
    eta = None
    if _ramp["active"] and _ramp["commanded"] is not None and _ramp["rate"]:
        eta = abs(_ramp["target"] - _ramp["commanded"]) / _ramp["rate"] * 60.0
    out = {
        "connected": bool(t and t["connected"]),
        "ramp": {"active": _ramp["active"], "target": _ramp["target"],
                 "rate": _ramp["rate"], "eta_s": eta, "error": _ramp["error"]},
    }
    if t:
        out.update({
            "temperature": _r(t["temperature"]),
            "setpoint": _r(t["setpoint"]),
            "unit": UNIT_NAMES.get(t["units"], "?"),
            "output": t["output_enabled"],
            "in_tolerance": t["in_tolerance"],
            "sensor_fault": t["sensor_fault"],
            "at_current_limit": t["at_current_limit"],
            "tec_current": _r(t["tec_current"]),
            "tec_voltage": _r(t["tec_voltage"]),
            "error": t["last_error"],
        })
    return out


@app.route("/temperature")
@login_required
def temperature():
    return jsonify(_temp_state())


@app.route("/temperature/target", methods=["POST"])
@login_required
def temperature_target():
    """Set a target. rate <= 0 jumps straight there; rate > 0 ramps (°/min)."""
    d = request.get_json() or {}
    try:
        target = float(d["celsius"])
        rate = float(d.get("rate") or 0)
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Need 'celsius' (and optional 'rate')"}), 400

    _ramp_cancel()
    _ramp.update(error="", target=target, rate=max(0.0, rate))
    try:
        if rate <= 0:
            scope.temperature.setpoint(target)
            _ramp.update(active=False, commanded=target)
            return jsonify({"message": f"Setpoint {target:g}", **_temp_state()})
        # Start from where the controller actually is, read live -- telemetry
        # may lag a previous command by up to a publish period.
        start = float(scope.temperature.setpoint())
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503

    stop = _ramp["stop"] = threading.Event()
    _ramp.update(active=True, commanded=start)
    _ramp["thread"] = threading.Thread(target=_ramp_loop,
                                       args=(stop, start, target, rate), daemon=True)
    _ramp["thread"].start()
    return jsonify({"message": f"Ramping {start:g} → {target:g} at {rate:g}/min",
                    **_temp_state()})


@app.route("/temperature/stop", methods=["POST"])
@login_required
def temperature_stop():
    """Stop ramping and hold wherever the setpoint got to (does NOT touch the
    output -- stopping a ramp is not an emergency stop)."""
    _ramp_cancel()
    return jsonify({"message": "Ramp stopped", **_temp_state()})


@app.route("/temperature/output", methods=["POST"])
@login_required
def temperature_output():
    on = bool((request.get_json() or {}).get("on"))
    if not on:
        _ramp_cancel()          # a ramp with the TEC off is meaningless
    try:
        scope.temperature.output(on)
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"message": "Output " + ("on" if on else "off"), **_temp_state()})


# ========== Client-side recording ==========
def _next_index():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    nums = [int(m.group(1)) for n in os.listdir(RECORDINGS_DIR)
            if (m := re.match(r"recording_(\d+)_", n))]
    return (max(nums) + 1) if nums else 1


def _record_loop(path, fps, duration):
    """Write the ingested JPEG stream to an MP4 until stopped / duration up."""
    import cv2
    import numpy as np
    writer = None
    interval = 1.0 / max(1.0, fps)
    start = time.time()
    try:
        while not _rec["stop"].is_set():
            if duration and time.time() - start >= duration:
                break
            with state.lock:
                jpeg = state.jpeg
            if jpeg is not None:
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    if writer is None:
                        h, w = frame.shape[:2]
                        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"),
                                                 fps, (w, h))
                    writer.write(frame)
            time.sleep(interval)
    finally:
        if writer is not None:
            writer.release()
        elapsed = max(1, int(round(time.time() - start)))
        # Rename to actual length.
        try:
            new = re.sub(r"_(\d+s|inf)\.mp4$", f"_{elapsed}s.mp4", path)
            if new != path and os.path.exists(path):
                os.rename(path, new)
        except OSError:
            pass
        _rec["active"] = False


@app.route("/set_recording_setting", methods=["POST"])
@login_required
def set_recording_setting():
    global record_duration
    d = request.get_json() or {}
    if d.get("setting") == "duration":
        v = d.get("value")
        record_duration = None if not v else max(1, int(v))
        return "OK"
    return "Invalid setting", 400


@app.route("/start_recording", methods=["POST"])
@login_required
def start_recording():
    if _rec["active"]:
        return jsonify({"error": "Already recording"}), 409
    with state.lock:
        cs = state.camera
    fps = round(state.stream_fps) or (round(cs["measured_fps"] or cs["target_fps"])
                                      if cs else 15)
    fps = fps or 15
    idx = _next_index()
    dur = record_duration
    name = f"recording_{idx}_{int(fps)}fps_{int(dur)}s.mp4" if dur else \
           f"recording_{idx}_{int(fps)}fps_inf.mp4"
    path = os.path.join(RECORDINGS_DIR, name)
    _rec.update(active=True, filename=name, started_at=time.time(), duration=dur)
    _rec["stop"].clear()
    _rec["thread"] = threading.Thread(target=_record_loop, args=(path, fps, dur), daemon=True)
    _rec["thread"].start()
    return jsonify({"filename": name, "duration": dur})


@app.route("/stop_recording", methods=["POST"])
@login_required
def stop_recording():
    if not _rec["active"]:
        return jsonify({"error": "Not recording"}), 400
    _rec["stop"].set()
    return jsonify({"message": "Recording stopped"})


@app.route("/recording_status")
@login_required
def recording_status():
    remaining = None
    if _rec["active"] and _rec["duration"]:
        remaining = max(0, int(_rec["duration"] - (time.time() - _rec["started_at"])))
    return jsonify({"recording": _rec["active"], "duration": _rec["duration"],
                    "remaining": remaining})


# ---- recordings library (operates on THIS app's local folder) ----
def _safe(name):
    if not name or name != os.path.basename(name) or not name.lower().endswith(".mp4"):
        return None
    return name


def _probe(path):
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    key = os.path.basename(path)
    c = _probe_cache.get(key)
    if c and c[0] == st.st_mtime and c[1] == st.st_size:
        return c[2], c[3]
    duration = fps = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=avg_frame_rate,nb_frames,duration", "-of", "json", path],
            capture_output=True, text=True, timeout=20)
        s = (json.loads(out.stdout or "{}").get("streams") or [{}])[0]
        duration = float(s.get("duration") or 0) or None
        nb = s.get("nb_frames")
        if nb and duration:
            fps = round(int(nb) / duration, 1)
        else:
            num, _, den = s.get("avg_frame_rate", "0/0").partition("/")
            if den and float(den):
                fps = round(float(num) / float(den), 1)
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError, OSError):
        pass
    _probe_cache[key] = (st.st_mtime, st.st_size, duration, fps)
    return duration, fps


@app.route("/recordings")
@login_required
def recordings():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    items = []
    for n in os.listdir(RECORDINGS_DIR):
        if not n.lower().endswith(".mp4"):
            continue
        p = os.path.join(RECORDINGS_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        dur, fps = _probe(p)
        items.append({"name": n, "duration": dur, "fps": fps,
                      "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(items)


@app.route("/recordings/file/<path:name>")
@login_required
def recordings_file(name):
    safe = _safe(name)
    if safe is None:
        abort(404)
    return send_from_directory(RECORDINGS_DIR, safe, conditional=True)


@app.route("/recordings/delete", methods=["POST"])
@login_required
def recordings_delete():
    safe = _safe((request.get_json() or {}).get("name", ""))
    if safe is None:
        return jsonify({"error": "Invalid name"}), 400
    try:
        os.remove(os.path.join(RECORDINGS_DIR, safe))
    except OSError as e:
        return jsonify({"error": str(e)}), 404
    _probe_cache.pop(safe, None)
    return jsonify({"message": "deleted"})


@app.route("/recordings/rename", methods=["POST"])
@login_required
def recordings_rename():
    d = request.get_json() or {}
    safe = _safe(d.get("name", ""))
    if safe is None:
        return jsonify({"error": "Invalid name"}), 400
    base = re.sub(r"[^A-Za-z0-9 _.\-]", "", os.path.splitext(os.path.basename(d.get("new_name", "")))[0]).strip()
    if not base:
        return jsonify({"error": "Empty name"}), 400
    new = base + ".mp4"
    src = os.path.join(RECORDINGS_DIR, safe)
    dst = os.path.join(RECORDINGS_DIR, new)
    if not os.path.exists(src):
        return jsonify({"error": "Not found"}), 404
    if os.path.exists(dst) and dst != src:
        return jsonify({"error": "A recording with that name already exists"}), 409
    try:
        os.rename(src, dst)
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"name": new})


# ========== main ==========
def main():
    global scope
    if not SCOPIO_API_KEY:
        raise SystemExit("Set SCOPIO_API_KEY (generate one on the Pi with "
                         "ros2_ws/scripts/generate_api_key.py) and SCOPIO_URL.")
    scope = Scopio(SCOPIO_URL, api_key=SCOPIO_API_KEY)
    threading.Thread(target=_subscribe_loop, daemon=True).start()
    threading.Thread(target=_frame_ingest_loop, daemon=True).start()
    log.info(f"SCOPIO UI on http://0.0.0.0:{PORT} -> microscope {SCOPIO_URL}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
