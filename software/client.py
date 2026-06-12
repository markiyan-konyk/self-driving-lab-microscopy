"""Flask application: the web server, page rendering, and all non-auth routes.

Creates the ``app``, wires in the auth blueprint, and exposes the HTTP API the
frontend talks to. Each route delegates to the relevant module
(``camera`` / ``controls`` / ``ML``) and reads/writes shared state through the
module namespace so cross-module updates stay visible.
"""

import os
import time
import threading

from flask import Flask, Response, jsonify, render_template_string, request

import camera
import controls
import ML
import authentification as auth

# ========== Server config ==========
# FIX #4: 변수명 명확화 (server → SERVER_PORT)
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
        record_framerate=camera.record_framerate,
        cam=camera.cam_controls,
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


# ========== Tracking settings ==========
@app.route("/set_tracking", methods=["POST"])
@auth.login_required
def set_tracking():
    data = request.get_json()
    ML.tracking_enabled = data.get("enabled", True)
    return "OK"


@app.route("/set_tracking_interval", methods=["POST"])
@auth.login_required
def set_tracking_interval():
    data = request.get_json()
    ML.tracking_interval_ms = data.get("interval_ms", 100)
    ML.tracking_interval_sec = ML.tracking_interval_ms / 1000.0
    return "OK"


# ========== Recording settings ==========
@app.route("/set_recording_setting", methods=["POST"])
@auth.login_required
def set_recording_setting():
    data = request.get_json()
    setting = data.get("setting")
    value = data.get("value")
    if setting == "duration":
        camera.record_duration = max(1, int(value))
    elif setting == "framerate":
        camera.record_framerate = max(1, int(value))
    else:
        return "Invalid setting", 400
    return "OK"


# ========== Camera controls ==========
@app.route("/set_camera_controls", methods=["POST"])
@auth.login_required
def set_camera_controls():
    data = request.get_json()
    for key in camera.cam_controls:
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
    # FIX #3: 원자적 check-and-set
    if not camera._try_acquire_calibration():
        return jsonify({"error": "Calibration already in progress"}), 409
    stage_wrapper = controls.SangaboardWrapper(controls.sb)
    thread = threading.Thread(target=camera.run_autofocus_thread, args=(stage_wrapper,), daemon=True)
    thread.start()
    return jsonify({"message": "Autofocus started"})


@app.route("/white_balance", methods=["POST"])
@auth.login_required
def white_balance():
    # FIX #3: 원자적 check-and-set
    if not camera._try_acquire_calibration():
        return jsonify({"error": "Calibration already in progress"}), 409
    thread = threading.Thread(target=camera.run_white_balance_thread, daemon=True)
    thread.start()
    return jsonify({"message": "White balance started"})


# ========== Streams ==========
@app.route("/tracking_stream")
@auth.login_required
def tracking_stream():
    headers = {
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    return Response(ML.event_stream(), mimetype="text/event-stream", headers=headers)


@app.route("/video_feed")
@auth.login_required
def video_feed():
    def generate():
        while True:
            # FIX #5: Lock으로 current_jpeg 읽기 보호
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
            time.sleep(1 / 15)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ========== Recording ==========
@app.route("/start_recording", methods=["POST"])
@auth.login_required
def start_recording():
    if camera.record_framerate < 1 or camera.record_duration < 1:
        return jsonify({"error": "Invalid parameters"}), 400
    if camera.is_recording:
        return jsonify({"error": "Already recording"}), 409
    rec_dir = "recordings"
    os.makedirs(rec_dir, exist_ok=True)
    idx = camera.get_next_recording_index()
    filename = f"recording_{idx}_{camera.record_framerate}fps_{camera.record_duration}s.mp4"
    filepath = os.path.join(rec_dir, filename)
    thread = threading.Thread(
        target=camera.start_recording_async,
        args=(camera.record_duration, camera.record_framerate, filepath),
        daemon=True,
    )
    thread.start()
    return jsonify({"filename": filename})


@app.route("/stop_recording", methods=["POST"])
@auth.login_required
def stop_recording():
    """FIX #9: 녹화 조기 종료 엔드포인트 (신규 추가)."""
    if not camera.is_recording:
        return jsonify({"error": "Not recording"}), 400
    camera._stop_recording_event.set()
    return jsonify({"message": "Recording stopped"})


# ========== Server ==========
def run_server():
    print("======= Microscope Controller =======")
    print(f"Flask server on http://0.0.0.0:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, use_reloader=False, threaded=True)
