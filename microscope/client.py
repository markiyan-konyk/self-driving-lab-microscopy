"""Flask application: the web server, page rendering, and all non-auth routes.

Creates the ``app``, wires in the auth blueprint, and exposes the HTTP API the
frontend talks to. Each route delegates to the relevant module
(``camera`` / ``controls``) and reads/writes shared state through the module
namespace so cross-module updates stay visible.
"""

import os
import time
import threading

from flask import Flask, Response, jsonify, render_template_string, request

import camera
import controls
import authentification as auth

# ========== Server config ==========
SERVER_PORT = int(os.environ.get("MICROSCOPE_PORT", 8000))

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

app = Flask(__name__)
app.secret_key = auth.SESSION_SECRET
app.register_blueprint(auth.bp)


def _read_frontend(name):
    with open(os.path.join(_FRONTEND_DIR, name), encoding="utf-8") as f:
        return f.read()


def render_page():
    """Stitch index.html + style.css + app.js, then render the Jinja vars.

    Stitching happens before render_template_string so every ``{{ }}``
    placeholder (in markup *and* in app.js) resolves in one pass.
    """
    html = _read_frontend("index.html")
    css = _read_frontend("style.css")
    js = _read_frontend("app.js")
    document = html.replace("__STYLE__", css).replace("__SCRIPT__", js)
    return render_template_string(
        document,
        steps=controls.steps,
        record_duration=camera.record_duration,
        cam=camera.cam_controls,
        min_fps=camera.MIN_FPS,
        max_fps=camera.MAX_FPS,
    )


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


# ========== Server ==========
def run_server():
    print("======= Microscope Controller =======")
    print(f"Flask server on http://0.0.0.0:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, use_reloader=False, threaded=True)
