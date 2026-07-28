#!/usr/bin/env python3
"""SCOPIO Web UI - a standalone API CLIENT application.

This program owns NO hardware and speaks NO ROS. It is the reference UI for
the SCOPIO microscope and talks to the Pi's API gateway over plain HTTP/
WebSocket through the scopio_client SDK:

  subscribes (WS):  camera/state, stage/position, calibration
  video (MJPEG):    /api/v1/stream.mjpg  -> live view + client-side recording
  services (HTTP):  stage/jog, calibration/set, camera controls
  action (WS):      camera/autofocus (runs on the backend)

Because it is a plain HTTP client it runs on ANY machine that can reach the
Pi -- no Docker, no WSL2, no DDS, no firewall rules. Several people can run
their own UI against the same microscope at once.

Recording happens HERE, client-side: the ingested JPEG stream is written to
MP4 in THIS app's own ./recordings folder, so footage lives with whoever runs
the UI, and the Pi takes no recording/disk load.

Run:
    pip install -r requirements.txt        # includes -e ../scopio_client
    # edit ui/.env: SCOPIO_URL=http://<pi-ip>:8000  and  SCOPIO_API_KEY=<key>
    python run_ui.py                       # serves http://0.0.0.0:8080

Config comes from ui/.env (loaded automatically); a real environment variable
overrides the file. SCOPIO_URL + SCOPIO_API_KEY are required; the key is minted
on the Pi with ros2_ws/scripts/generate_api_key.py. Optional: SCOPIO_UI_PORT
(8080), SCOPIO_UI_PASSWORD ("password").
"""

import os
import re
import json
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
        self.awg = None             # awg/status message dict (galvo wavegen)
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
            scope.subscribe("calibration", store("calibration"))
            state.connected = True
            log.info("subscribed to microscope telemetry")
            return
        except ScopioError as e:
            log.warning(f"cannot subscribe yet ({e}); retrying in 3 s")
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
        st, cs = state.stage, state.camera
    pos = {"x": st["x"], "y": st["y"], "z": st["z"]} if st else {"x": 0, "y": 0, "z": 0}
    fps = round(state.stream_fps, 1) or (cs["measured_fps"] if cs else 0.0)
    return jsonify({
        "position": pos,
        "fps": fps,
        "target_fps": cs["target_fps"] if cs else 0.0,
        "controller_connected": bool(st and st["connected"]),
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
    return bool(awg and awg["connected"])


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
