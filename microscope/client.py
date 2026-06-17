"""Flask application: the web server, page rendering, and all non-auth routes.

Creates the ``app``, wires in the auth blueprint, and exposes the HTTP API the
frontend talks to. Each route delegates to the relevant module
(``camera`` / ``controls``) and reads/writes shared state through the module
namespace so cross-module updates stay visible.
"""

import os
import re
import json
import time
import threading
import subprocess

from flask import (
    Flask, Response, jsonify, render_template_string, request,
    send_from_directory, abort,
)

import camera
import controls
import authentification as auth

# ========== Server config ==========
SERVER_PORT = int(os.environ.get("MICROSCOPE_PORT", 8000))

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
RECORDINGS_DIR = "recordings"
CALIBRATION_FILE = "calibration.json"

# ffprobe cache: filename -> (mtime, size, duration_sec, fps)
_probe_cache = {}
_probe_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = auth.SESSION_SECRET
app.register_blueprint(auth.bp)


def _read_frontend(name):
    with open(os.path.join(_FRONTEND_DIR, name), encoding="utf-8") as f:
        return f.read()


def render_page():
    """Stitch index.html + style.css + app.js (+ logo), then render Jinja vars.

    Stitching happens before render_template_string so every ``{{ }}``
    placeholder (in markup *and* in app.js) resolves in one pass.
    """
    html = _read_frontend("index.html")
    css = _read_frontend("style.css")
    js = _read_frontend("app.js")
    logo = _read_frontend("logo.svg")
    document = (html.replace("__STYLE__", css)
                    .replace("__SCRIPT__", js)
                    .replace("__LOGO__", logo))
    return render_template_string(
        document,
        steps=controls.steps,
        record_duration=camera.record_duration,
        cam=camera.cam_controls,
        min_fps=camera.MIN_FPS,
        max_fps=camera.MAX_FPS,
    )


# ========== Recording / calibration helpers ==========
def _safe_recording_name(name):
    """Return a safe basename for a file that must live in RECORDINGS_DIR, or
    None if the name is unsafe (path traversal, wrong extension, etc.)."""
    if not name or name != os.path.basename(name):
        return None
    if not name.lower().endswith(".mp4"):
        return None
    return name


def _probe_recording(path):
    """Return (duration_seconds, real_fps) for a video via ffprobe, cached by
    file mtime+size. Falls back to (None, None) if ffprobe is unavailable."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    key = os.path.basename(path)
    with _probe_lock:
        cached = _probe_cache.get(key)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2], cached[3]

    duration, fps = None, None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,nb_frames,duration",
             "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=20,
        )
        info = json.loads(out.stdout or "{}")
        stream = (info.get("streams") or [{}])[0]
        fmt = info.get("format") or {}
        duration = float(stream.get("duration") or fmt.get("duration") or 0) or None
        # Prefer frames/duration (true average) over the container's nominal rate.
        nb = stream.get("nb_frames")
        if nb and duration:
            fps = round(int(nb) / duration, 1)
        else:
            afr = stream.get("avg_frame_rate", "0/0")
            num, _, den = afr.partition("/")
            if den and float(den) != 0:
                fps = round(float(num) / float(den), 1)
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError, OSError) as e:
        print(f"ffprobe failed for {path}: {e}")

    with _probe_lock:
        _probe_cache[key] = (st.st_mtime, st.st_size, duration, fps)
    return duration, fps


def _load_calibration():
    try:
        with open(CALIBRATION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"um_per_px": None}


# ========== Pages ==========
@app.route("/")
@auth.login_required
def index():
    return render_page()


@app.route("/status")
@auth.login_required
def status():
    return jsonify({
        "controller_connected": controls.sb is not None,
        "controller_error": controls.controller_error,
        "steps": controls.steps,
    })


# ========== Motor control ==========
@app.route("/move/<direction>")
@auth.login_required
def move(direction):
    body, code = controls.move_motor(direction)
    return body, code


@app.route("/adjust/<axis>/<op>")
@auth.login_required
def adjust(axis, op):
    body, code = controls.adjust_step(axis, op)
    return body, code


@app.route("/set_step/<axis>/<int:value>")
@auth.login_required
def set_step(axis, value):
    body, code = controls.set_step(axis, value)
    return body, code


# ========== Recording settings ==========
@app.route("/set_recording_setting", methods=["POST"])
@auth.login_required
def set_recording_setting():
    data = request.get_json()
    setting = data.get("setting")
    value = data.get("value")
    if setting == "duration":
        # 0 / null means "record until manually stopped" (infinite).
        camera.record_duration = None if not value else max(1, int(value))
    else:
        return "Invalid setting", 400
    return "OK"


# ========== Camera controls ==========
@app.route("/set_framerate", methods=["POST"])
@auth.login_required
def set_framerate():
    """Single user-facing frame-rate control. The exposure and the analogue
    gain needed to achieve it (without darkening) are derived server-side."""
    data = request.get_json()
    fps = data.get("fps")
    if fps is None:
        return "Missing fps", 400
    camera.apply_framerate(float(fps))
    return jsonify({
        "framerate": camera.cam_controls["framerate"],
        "exposure": camera.cam_controls["exposure"],
        "analogue_gain": camera.cam_controls["analogue_gain"],
    })


@app.route("/set_camera_controls", methods=["POST"])
@auth.login_required
def set_camera_controls():
    data = request.get_json()
    # A manual analogue-gain change is interpreted as a brightness change so it
    # survives subsequent frame-rate changes.
    if "analogue_gain" in data:
        camera.set_exposure_budget_from_gain(float(data["analogue_gain"]))
    for key in camera.cam_controls:
        if key in ("framerate", "exposure", "analogue_gain"):
            continue
        if key in data:
            camera.cam_controls[key] = float(data[key])
    with camera.camera_lock:
        camera.apply_camera_controls()
    return "OK"


# ========== Calibration ==========
@app.route("/autofocus", methods=["POST"])
@auth.login_required
def autofocus():
    if controls.sb is None:
        return jsonify({"error": "Sangaboard not connected"}), 503
    if not camera._try_acquire_calibration():
        return jsonify({"error": "Calibration already in progress"}), 409
    stage_wrapper = controls.SangaboardWrapper(controls.sb)
    thread = threading.Thread(target=camera.run_autofocus_thread, args=(stage_wrapper,), daemon=True)
    thread.start()
    return jsonify({"message": "Autofocus started"})


@app.route("/white_balance", methods=["POST"])
@auth.login_required
def white_balance():
    if not camera._try_acquire_calibration():
        return jsonify({"error": "Calibration already in progress"}), 409
    thread = threading.Thread(target=camera.run_white_balance_thread, daemon=True)
    thread.start()
    return jsonify({"message": "White balance started"})


@app.route("/calibration_status")
@auth.login_required
def calibration_status():
    """Polled by the UI so the calibration buttons reflect the real state
    (the /autofocus and /white_balance routes return immediately)."""
    return jsonify({"running": camera.calibration_running})


@app.route("/get_camera_controls")
@auth.login_required
def get_camera_controls():
    """Current cam_controls; the UI re-syncs its sliders from this after a
    calibration so the calibrated gains aren't overwritten by stale values."""
    return jsonify(camera.cam_controls)


# ========== Streams ==========
@app.route("/video_feed")
@auth.login_required
def video_feed():
    def generate():
        while True:
            with camera._jpeg_lock:
                jpeg = camera.current_jpeg
            if jpeg is None:
                time.sleep(0.02)
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" +
                jpeg + b"\r\n"
            )
            time.sleep(1 / camera.DISPLAY_FPS)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ========== Recording ==========
@app.route("/start_recording", methods=["POST"])
@auth.login_required
def start_recording():
    if camera.is_recording:
        return jsonify({"error": "Already recording"}), 409
    rec_dir = "recordings"
    os.makedirs(rec_dir, exist_ok=True)
    idx = camera.get_next_recording_index()
    fps = int(round(camera.cam_controls["framerate"]))
    dur = camera.record_duration
    dur_tag = f"{int(dur)}s" if dur else "inf"
    filename = f"recording_{idx}_{fps}fps_{dur_tag}.mp4"
    filepath = os.path.join(rec_dir, filename)
    thread = threading.Thread(
        target=camera.start_recording_async,
        args=(camera.record_duration, filepath),
        daemon=True,
    )
    thread.start()
    return jsonify({"filename": filename, "duration": dur})


@app.route("/stop_recording", methods=["POST"])
@auth.login_required
def stop_recording():
    if not camera.is_recording:
        return jsonify({"error": "Not recording"}), 400
    camera._stop_recording_event.set()
    return jsonify({"message": "Recording stopped"})


@app.route("/recording_status")
@auth.login_required
def recording_status():
    """Lets a freshly-loaded client (e.g. a phone joining mid-recording)
    discover that a recording is in progress and how much time is left."""
    remaining = None
    if camera.is_recording and camera.recording_started_at and camera.recording_duration:
        elapsed = time.time() - camera.recording_started_at
        remaining = max(0, int(camera.recording_duration - elapsed))
    return jsonify({
        "recording": camera.is_recording,
        "duration": camera.recording_duration,
        "remaining": remaining,
    })


# ========== Telemetry ==========
@app.route("/telemetry")
@auth.login_required
def telemetry():
    """Absolute stage position + the real measured frame rate, for the HUD."""
    return jsonify({
        "position": controls.position,
        "fps": camera.measured_fps,
        "target_fps": camera.cam_controls["framerate"],
        "controller_connected": controls.sb is not None,
    })


# ========== Recordings library ==========
@app.route("/recordings")
@auth.login_required
def list_recordings():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    items = []
    for name in os.listdir(RECORDINGS_DIR):
        if not name.lower().endswith(".mp4"):
            continue
        path = os.path.join(RECORDINGS_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        duration, fps = _probe_recording(path)
        items.append({
            "name": name,
            "duration": duration,
            "fps": fps,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(items)


@app.route("/recordings/file/<path:name>")
@auth.login_required
def recording_file(name):
    safe = _safe_recording_name(name)
    if safe is None:
        abort(404)
    # send_from_directory supports HTTP Range, so the preview player can seek.
    return send_from_directory(RECORDINGS_DIR, safe, conditional=True)


@app.route("/recordings/delete", methods=["POST"])
@auth.login_required
def delete_recording():
    name = (request.get_json() or {}).get("name", "")
    safe = _safe_recording_name(name)
    if safe is None:
        return jsonify({"error": "Invalid name"}), 400
    path = os.path.join(RECORDINGS_DIR, safe)
    if camera.is_recording and camera.current_recording_filename == os.path.splitext(safe)[0]:
        return jsonify({"error": "Cannot delete a recording in progress"}), 409
    try:
        os.remove(path)
    except OSError as e:
        return jsonify({"error": str(e)}), 404
    with _probe_lock:
        _probe_cache.pop(safe, None)
    return jsonify({"message": "deleted"})


@app.route("/recordings/rename", methods=["POST"])
@auth.login_required
def rename_recording():
    data = request.get_json() or {}
    safe = _safe_recording_name(data.get("name", ""))
    if safe is None:
        return jsonify({"error": "Invalid name"}), 400
    raw_new = (data.get("new_name") or "").strip()
    # Keep it a single safe .mp4 basename.
    base = re.sub(r"[^A-Za-z0-9 _.\-]", "", os.path.splitext(os.path.basename(raw_new))[0]).strip()
    if not base:
        return jsonify({"error": "Empty name"}), 400
    new_name = base + ".mp4"
    src = os.path.join(RECORDINGS_DIR, safe)
    dst = os.path.join(RECORDINGS_DIR, new_name)
    if not os.path.exists(src):
        return jsonify({"error": "Not found"}), 404
    if os.path.exists(dst) and dst != src:
        return jsonify({"error": "A recording with that name already exists"}), 409
    try:
        os.rename(src, dst)
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    with _probe_lock:
        if safe in _probe_cache:
            _probe_cache[new_name] = _probe_cache.pop(safe)
    return jsonify({"name": new_name})


# ========== Calibration (px <-> micrometres), persisted to disk ==========
@app.route("/get_calibration")
@auth.login_required
def get_calibration():
    return jsonify(_load_calibration())


@app.route("/set_calibration", methods=["POST"])
@auth.login_required
def set_calibration():
    data = request.get_json() or {}
    try:
        px = float(data["pixels"])
        um = float(data["micrometres"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Need 'pixels' and 'micrometres'"}), 400
    if px <= 0 or um <= 0:
        return jsonify({"error": "Values must be positive"}), 400
    record = {
        "um_per_px": um / px,
        "ref_pixels": px,
        "ref_micrometres": um,
        "updated": time.time(),
    }
    try:
        with open(CALIBRATION_FILE, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(record)


# ========== Server ==========
def run_server():
    print("======= Microscope Controller =======")
    print(f"Flask server on http://0.0.0.0:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, use_reloader=False, threaded=True)
