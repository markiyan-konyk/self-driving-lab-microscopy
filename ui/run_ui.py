#!/usr/bin/env python3
"""SCOPIO Web UI - a standalone API CLIENT application.

This program owns NO hardware and speaks NO ROS. It is the reference UI for
the SCOPIO microscope and talks to the Pi's API gateway over plain HTTP/
WebSocket through the scopio_client SDK:

  subscribes (WS):  camera/state, stage/position, awg/status,
                    temperature/status, relay/state, calibration
  video (MJPEG):    /api/v1/stream.mjpg  -> live view + client-side recording
  services (HTTP):  stage/jog, calibration/set, relay/set, awg/call,
                    temperature/call, camera white balance
  action (WS):      camera/autofocus (runs on the backend)

Because it is a plain HTTP client it runs on ANY machine that can reach the
Pi -- no Docker, no WSL2, no DDS, no firewall rules. Several people can run
their own UI against the same microscope at once.

Recording happens HERE, client-side: the ingested JPEG stream is written to MP4
on the machine running THIS program (folder set from the UI, default
./recordings), so footage lives with whoever runs the UI and the Pi takes no
recording or disk load.

Run:
    pip install -r requirements.txt        # includes -e ../scopio_client
    # edit ui/.env: SCOPIO_URL=http://<pi-ip>:8000  and  SCOPIO_API_KEY=<key>
    python run_ui.py                       # serves http://0.0.0.0:8080

Config comes from ui/.env (loaded automatically); a real environment variable
overrides the file. SCOPIO_URL + SCOPIO_API_KEY are required; the key is minted
on the Pi with ros2_ws/scripts/generate_api_key.py. Optional: SCOPIO_UI_PORT
(8080), SCOPIO_UI_PASSWORD ("password"), SCOPIO_RECORDINGS_DIR.
"""

import os
import re
import time
import socket
import secrets
import logging
import threading
from functools import wraps

from flask import (
    Flask, Response, jsonify, render_template_string, request, session,
    redirect, url_for,
)

from scopio_client import Scopio, ScopioError

# ========== Config ==========
HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Load KEY=VALUE lines from a .env file into os.environ (no dependency).

    This is how the UI finds the microscope: put the Pi's address and your API
    key in ui/.env and just `python run_ui.py`. A real environment variable
    always wins over the file, so you can still override on the command line.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:      # real env vars take precedence
            os.environ[key] = value


_load_dotenv(os.path.join(HERE, ".env"))

FRONTEND_DIR = os.path.join(HERE, "frontend")
PORT = int(os.environ.get("SCOPIO_UI_PORT", 8080))
PASSWORD = os.environ.get("SCOPIO_UI_PASSWORD", "password")
SCOPIO_URL = os.environ.get("SCOPIO_URL", "http://127.0.0.1:8000")
SCOPIO_API_KEY = os.environ.get("SCOPIO_API_KEY", "")

# Where clips land, on THIS machine (the one running run_ui.py). Changeable
# from the UI at runtime -- see /recordings/dir.
recordings_dir = os.path.abspath(os.environ.get("SCOPIO_RECORDINGS_DIR")
                                 or os.path.join(HERE, "recordings"))

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
        self.jpeg_seq = 0           # bumped per frame; /video_feed waits on it
        self.stream_fps = 0.0       # measured fps of the ingested stream
        self.last_error = ""        # why the microscope is unreachable, for the UI
        self.camera = None          # camera/state message dict
        self.stage = None           # stage/position message dict
        self.awg = None             # awg/status message dict (galvo wavegen)
        self.temp = None            # temperature/status message dict (TC10 LAB)
        self.relay = None
        self.calibration = None     # calibration message dict (latched)
        self.connected = False      # gateway subscriptions established


state = State()
scope = None      # set in main()
steps = {"x": 40, "y": 40, "z": 40}     # client-side step sizes
record_duration = 600                    # seconds, or None for infinite
_wb_running = False
_af_running = False

# ---- client-side recording state ----
_rec = {"active": False, "thread": None, "stop": threading.Event(),
        "filename": None, "started_at": None, "duration": None}


def _read(name):
    with open(os.path.join(FRONTEND_DIR, name), encoding="utf-8") as f:
        return f.read()


# ---- background workers ----
# topic -> State attribute, and the rate to throttle it to (None = unthrottled).
TELEMETRY = (
    ("camera/state", "camera", 4),
    ("stage/position", "stage", 10),
    ("awg/status", "awg", 2),
    ("temperature/status", "temp", 2),
    ("relay/state", "relay", None),
    ("calibration", "calibration", None),
)


def _subscribe_loop():
    """Establish the WS subscriptions; retry until the gateway is reachable
    (so the UI comes up fine even if the Pi boots later)."""
    def store(attr):
        def cb(msg, _envelope):
            with state.lock:
                setattr(state, attr, msg)
        return cb

    done, warned = set(), set()
    while True:
        try:
            # One try PER topic, and never re-subscribe one that already took.
            # A backend older than this UI (no temperature_node, no relay_node)
            # answers `unknown_topic` for it, and letting that escape cost every
            # OTHER topic too: the whole batch was retried every 3 s, minting a
            # fresh subscription id each round, so the gateway piled up a
            # duplicate rclpy subscription per topic per round while the browser
            # showed nothing at all. Missing node => only ITS controls go dark.
            for topic, attr, rate in TELEMETRY:
                if topic in done:
                    continue
                try:
                    scope.subscribe(topic, store(attr), rate_hz=rate)
                    done.add(topic)
                    log.info(f"subscribed to {topic}")
                except ScopioError as e:
                    # Anything that is not "that node is absent" means the link
                    # itself is down; the handler below owns that case and says
                    # so once, instead of once per topic every 3 s.
                    if (e.payload or {}).get("code") != "unknown_topic":
                        raise
                    if topic not in warned:
                        warned.add(topic)
                        log.warning(f"no {topic} in the microscope graph; those "
                                    "controls stay offline until that node appears")
            # Heartbeat. This loop used to return the moment it subscribed, so
            # `connected` was a latch: once true it stayed true through every
            # later outage, and the reason a connection failed only ever
            # reached this log -- never the browser. /api/v1/health needs no
            # auth, so it separates "cannot reach the Pi" from "bad API key".
            scope.health()
            state.connected, state.last_error = True, ""
        except ScopioError as e:
            state.connected, state.last_error = False, str(e)
            log.warning(f"microscope unreachable ({e}); retrying in 3 s")
        time.sleep(3)


def _frame_ingest_loop():
    """Pull the live MJPEG stream through the gateway into state.jpeg -- feeds
    /video_feed, recording and the focus display. Reconnects forever."""
    last_t = time.time()
    while True:
        try:
            for frame in scope.stream_frames():
                with state.lock:
                    state.jpeg = frame
                    state.jpeg_seq += 1
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
    document = (_read("index.html")
                .replace("__STYLE__", _read("style.css"))
                .replace("__SCRIPT__", _read("app.js"))
                .replace("__LOGO__", _read("logo.svg")))
    return render_template_string(document, steps=steps)


@app.route("/health")
def health():
    """Can this UI reach the microscope, and if not, why.

    Deliberately NOT behind the login: a connection you cannot diagnose without
    first logging in is a connection you cannot diagnose. Carries no sample
    data -- only whether the link works.

    frames_ingested is the number that matters when video looks wrong. Compare
    it with the camera server's own `frames` (GET :8081/controls on the Pi):
    both climbing means the pixels are arriving and the problem is in the
    browser; the Pi's climbing while this one is stuck means the break is
    between them -- gateway, network or proxy.
    """
    with state.lock:
        seq, fps = state.jpeg_seq, round(state.stream_fps, 1)
    return jsonify({
        "scope_url": SCOPIO_URL,
        "scope_reachable": state.connected,
        "scope_error": state.last_error,
        "frames_ingested": seq,
        "stream_fps": fps,
    })


@app.route("/video_feed")
@login_required
def video_feed():
    def gen():
        # Send each frame ONCE. Re-sending state.jpeg on a timer meant a dead
        # ingest was indistinguishable from a live camera: the browser kept
        # receiving the same image at 15 fps and the picture just stopped
        # moving. Waiting on the sequence number means a stalled stream shows
        # as a stalled stream, and /telemetry says why.
        sent = -1
        while True:
            with state.lock:
                jpeg, seq = state.jpeg, state.jpeg_seq
            if jpeg is None or seq == sent:
                time.sleep(0.02)
                continue
            sent = seq
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/status")
@login_required
def status():
    with state.lock:
        st = state.stage
    return jsonify({"controller_connected": bool(st and st.get("connected")),
                    "steps": steps})


@app.route("/telemetry")
@login_required
def telemetry():
    with state.lock:
        st, cs = state.stage, state.camera
    pos = {a: (st or {}).get(a, 0) for a in ("x", "y", "z")}
    fps = round(state.stream_fps, 1) or (cs.get("measured_fps", 0.0) if cs else 0.0)
    return jsonify({
        "position": pos,
        "fps": fps,
        "target_fps": cs.get("target_fps", 0.0) if cs else 0.0,
        "controller_connected": bool(st and st.get("connected")),
        # Whether the UI can reach the MICROSCOPE at all, and why not. Distinct
        # from controller_connected, which is about the stage: on a network that
        # cannot route to the Pi everything reads "not connected" with no cause,
        # and this is the cause. `curl localhost:8080/telemetry` answers it.
        "scope_url": SCOPIO_URL,
        "scope_reachable": state.connected,
        "scope_error": state.last_error,
    })

# ---- relay (laser) ----
@app.route("/relay/status")
@login_required
def relay_status():
    with state.lock:
        current, live = state.relay, state.connected
    # Gated on `live`: state.relay is a cache nothing clears, so on its own it
    # keeps the button enabled and green on a last-known value hours after the
    # Pi went away. The galvo and temperature boxes read that liveness from a
    # `connected` field inside their message; a bare std_msgs/Bool has none.
    # `on` still reports the last known value -- an unreachable laser is unknown,
    # not off, and the button is disabled either way.
    on = bool(current and current.get("data", False))
    return jsonify({"available": bool(current is not None and live), "on": on})


@app.route("/relay/set", methods=["POST"])
@login_required
def relay_set():
    body = request.get_json(silent=True) or {}
    on = body.get("on")
    if not isinstance(on, bool):
        return jsonify({"error": "'on' must be true or false"}), 400
    try:
        result = scope.call_service("relay/set", {"data": on})
    except ScopioError as exc:
        return jsonify({"error": str(exc)}), 503
    if not result.get("success", False):
        return jsonify({"error": result.get("message", "Relay command failed")}), 503
    # Cache the new state so the button repaints without waiting for the topic;
    # relay/state confirms it a moment later (and wins if the node disagrees).
    with state.lock:
        state.relay = {"data": on}
    return jsonify({"available": True, "on": on,
                    "message": result.get("message", "")})


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


# ---- camera calibration (autofocus + one-shot white balance) ----
# The per-control camera sliders are deliberately NOT here: exposure, gains and
# frame rate are set once for a sample and then left alone, and a panel of them
# crowded out the controls an operator actually touches. Anything that needs
# them reaches the gateway directly (scope.camera.set_controls / the MCP tool).
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
    return jsonify({"um_per_px": c.get("um_per_px")})


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


# ---- galvo laser (X = CH1, Y = CH2 on the Rigol DG1022Z) ----
# The galvo_node owns the DG1022Z driver and exposes its methods over awg/call;
# we call update(ch, val) through the scopio_client SDK. The node runs dcinit()
# on connect (DC mode + outputs on), so update() positions the mirror right
# away. The wavegen buffer is small, so the UI sends ONE update per button
# press -- never on slider drag.
_galvo = {"x": 0.0, "y": 0.0}     # last commanded volts, for display
GALVO_V_MIN, GALVO_V_MAX = -5.0, 5.0


def _galvo_connected():
    with state.lock:
        awg = state.awg
    return bool(awg and awg.get("connected"))


@app.route("/galvo/status")
@login_required
def galvo_status():
    return jsonify({"connected": _galvo_connected(), "x": _galvo["x"], "y": _galvo["y"]})


@app.route("/galvo/update", methods=["POST"])
@login_required
def galvo_update():
    d = request.get_json() or {}
    try:
        x = max(GALVO_V_MIN, min(GALVO_V_MAX, float(d.get("x", 0.0))))
        y = max(GALVO_V_MIN, min(GALVO_V_MAX, float(d.get("y", 0.0))))
    except (TypeError, ValueError):
        return jsonify({"error": "x and y must be numbers"}), 400
    try:
        scope.galvo.call("update", 1, x)      # DG1022Z.update(ch=1, val=x)
        scope.galvo.call("update", 2, y)      # DG1022Z.update(ch=2, val=y)
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503
    _galvo["x"], _galvo["y"] = x, y
    return jsonify({"connected": _galvo_connected(), "x": x, "y": y})


# ---- sample temperature (Wavelength TC10 LAB) ----
# temperature_node owns the TC10LAB driver and exposes it over temperature/call.
# TWO separate commands, exactly like the instrument: set_setpoint only stores a
# target; output(True) is what actually drives the TEC. The reading comes from
# the cached status topic, so polling the UI costs the instrument nothing.
TEMP_MIN, TEMP_MAX = -20.0, 120.0


def _num(v):
    """NaN/inf -> None. The node publishes NaN while disconnected, and NaN is
    not valid JSON -- json.dumps emits a bare `NaN` that JSON.parse rejects."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _temp_payload():
    with state.lock:
        t = state.temp
    if not t:
        return {"connected": False, "temperature": None, "setpoint": None,
                "output": False, "in_tolerance": False, "units": "C", "faults": []}
    return {"connected": bool(t.get("connected")),
            "temperature": _num(t.get("temperature")), "setpoint": _num(t.get("setpoint")),
            "output": bool(t.get("output")), "in_tolerance": bool(t.get("in_tolerance")),
            "units": t.get("units") or "C", "faults": list(t.get("faults") or [])}


@app.route("/temperature/status")
@login_required
def temperature_status():
    return jsonify(_temp_payload())


@app.route("/temperature/set", methods=["POST"])
@login_required
def temperature_set():
    d = request.get_json() or {}
    try:
        sp = float(d.get("setpoint"))
    except (TypeError, ValueError):
        return jsonify({"error": "setpoint must be a number"}), 400
    if not (TEMP_MIN <= sp <= TEMP_MAX):
        return jsonify({"error": f"setpoint must be {TEMP_MIN:g}..{TEMP_MAX:g} °C"}), 400
    try:
        scope.temperature.setpoint(sp)
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503
    payload = _temp_payload()
    payload["setpoint"] = sp        # the status topic is up to a poll behind
    return jsonify(payload)


@app.route("/temperature/output", methods=["POST"])
@login_required
def temperature_output():
    on = bool((request.get_json() or {}).get("on"))
    try:
        scope.temperature.output(on)
    except ScopioError as e:
        return jsonify({"error": str(e)}), 503
    payload = _temp_payload()
    payload["output"] = on
    return jsonify(payload)


# ========== Client-side recording ==========
def _next_index(folder):
    nums = [int(m.group(1)) for n in os.listdir(folder)
            if (m := re.match(r"recording_(\d+)_", n))]
    return (max(nums) + 1) if nums else 1


def _record_loop(path, fps, duration):
    """Write the ingested JPEG stream to an MP4 until stopped / duration up.

    ONE written frame per INGESTED frame -- it waits on jpeg_seq, exactly like
    /video_feed. Sampling state.jpeg on a timer instead writes the same frame
    twice when the camera runs slower than fps and skips frames when it runs
    faster, so the clip's timebase stops matching real time. That timebase IS
    the measurement in any motion analysis done on the footage later.
    """
    import cv2
    import numpy as np
    writer, written, last_seq = None, 0, -1
    start = time.time()
    try:
        while not _rec["stop"].is_set():
            if duration and time.time() - start >= duration:
                break
            with state.lock:
                jpeg, seq = state.jpeg, state.jpeg_seq
            if jpeg is None or seq == last_seq:
                time.sleep(0.005)
                continue
            last_seq = seq
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            if writer is None:
                h, w = frame.shape[:2]
                writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"),
                                         fps, (w, h))
                if not writer.isOpened():
                    # VideoWriter reports this ONLY here: write() on a writer
                    # that never opened is a silent no-op and leaves no file.
                    log.error(f"cannot open {path} for writing; recording stopped")
                    return
            writer.write(frame)
            written += 1
    finally:
        if writer is not None:
            writer.release()
        _finish_recording(path, written, time.time() - start)


def _finish_recording(path, frames, elapsed):
    """Rename the clip to the length it actually ran, and report where it is."""
    _rec["active"] = False
    if not frames:
        log.warning("recording produced no frames; no file was written")
        return
    try:
        new = re.sub(r"_(\d+s|inf)\.mp4$", f"_{max(1, round(elapsed))}s.mp4", path)
        if new != path and os.path.exists(path):
            os.rename(path, new)
            path = new
    except OSError:
        pass
    _rec["filename"] = os.path.basename(path)
    log.info(f"saved {path}  ({frames} frames, {elapsed:.1f} s)")


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
    # The WRITER THREAD is the interlock, not _rec["active"]: the flag is set
    # False by the thread as it exits, so between /stop_recording returning and
    # the MP4 actually closing, the flag and reality disagree in both directions
    # -- a legitimate restart was refused as "already recording", and a second
    # writer could start while the first still held the file.
    previous = _rec["thread"]
    if previous is not None and previous.is_alive():
        if not _rec["stop"].is_set():
            return jsonify({"error": "Already recording"}), 409
        previous.join(timeout=10.0)         # stopping: let it close the file
        if previous.is_alive():
            return jsonify({"error": "The previous clip is still being written"}), 409
    with state.lock:
        cs, have_frames = state.camera, state.jpeg is not None
    if not have_frames:
        # Without this the writer is never created, the file never appears, and
        # the UI counts down a recording that was never happening.
        return jsonify({"error": "No video from the microscope yet"}), 503
    try:
        folder = _ensure_dir(recordings_dir)
    except OSError as e:
        return jsonify({"error": f"Cannot write to {recordings_dir}: {e}"}), 400
    # Clamped: fps is the clip's TIMEBASE, and any motion analysis done on the
    # footage later is only as good as it. A reconnect that delivers a burst of
    # buffered frames spikes the measured rate well past anything real.
    measured = state.stream_fps or (cs.get("measured_fps") or cs.get("target_fps")
                                    if cs else 0)
    fps = min(120, max(1, round(measured or 15)))
    dur = record_duration
    tail = f"{int(dur)}s" if dur else "inf"
    name = f"recording_{_next_index(folder)}_{int(fps)}fps_{tail}.mp4"
    path = os.path.join(folder, name)
    _rec.update(active=True, filename=name, started_at=time.time(), duration=dur)
    _rec["stop"].clear()
    _rec["thread"] = threading.Thread(target=_record_loop, args=(path, fps, dur),
                                      daemon=True)
    _rec["thread"].start()
    return jsonify({"filename": name, "path": path, "duration": dur})


@app.route("/stop_recording", methods=["POST"])
@login_required
def stop_recording():
    if not _rec["active"]:
        return jsonify({"error": "Not recording"}), 400
    _rec["stop"].set()
    # WAIT for the writer to close the file before answering. Returning early
    # left /recording_status reporting "recording" for another poll or two --
    # long enough for the browser to flip the button back to "Stop" and restart
    # the timer -- and meant the reply could not name the file it just saved.
    thread = _rec["thread"]
    if thread is not None:
        thread.join(timeout=15.0)
    return jsonify({"message": "Recording stopped", "filename": _rec["filename"],
                    "still_writing": bool(thread and thread.is_alive())})


@app.route("/recording_status")
@login_required
def recording_status():
    remaining = None
    if _rec["active"] and _rec["duration"]:
        remaining = max(0, int(_rec["duration"] - (time.time() - _rec["started_at"])))
    return jsonify({"recording": _rec["active"], "duration": _rec["duration"],
                    "remaining": remaining, "filename": _rec["filename"]})


# ---- where clips are saved ----
# On the machine running THIS program, not on the Pi and not in the browser:
# the frames are ingested here, so this is the only disk they can reach. Several
# people may each run their own UI against one microscope, and each keeps its
# own folder.
def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise OSError("no write permission")
    return path


@app.route("/recordings/dir", methods=["GET", "POST"])
@login_required
def recordings_dir_route():
    global recordings_dir
    if request.method == "POST":
        if _rec["active"]:
            return jsonify({"error": "Stop the recording first"}), 409
        raw = str((request.get_json(silent=True) or {}).get("path", "")).strip()
        if not raw:
            return jsonify({"error": "Give a folder path"}), 400
        path = os.path.abspath(os.path.expanduser(raw))
        try:
            _ensure_dir(path)
        except OSError as e:
            return jsonify({"error": f"{path}: {e}"}), 400
        recordings_dir = path
    return jsonify({"path": recordings_dir, "host": socket.gethostname()})


# ========== main ==========
def main():
    global scope
    if not SCOPIO_API_KEY:
        raise SystemExit(
            "SCOPIO_API_KEY is not set. Put it (and SCOPIO_URL=http://<pi-ip>:8000) "
            "in ui/.env. Mint a key on the Pi with ros2_ws/scripts/generate_api_key.py.")
    scope = Scopio(SCOPIO_URL, api_key=SCOPIO_API_KEY)
    threading.Thread(target=_subscribe_loop, daemon=True).start()
    threading.Thread(target=_frame_ingest_loop, daemon=True).start()
    log.info(f"SCOPIO UI on http://0.0.0.0:{PORT} -> microscope {SCOPIO_URL}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
