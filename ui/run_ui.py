#!/usr/bin/env python3
"""SCOPIO Web UI - a standalone ROS 2 CLIENT application.

This program owns NO hardware. It is the reference UI for the SCOPIO microscope
and it talks to the backend (the ros2_ws driver nodes) purely over ROS:

  subscribes:  image/compressed, camera/state, stage/position, awg/status,
               beads, calibration
  calls srv:   camera/set_controls, camera/set_framerate, camera/white_balance,
               stage/jog, calibration/set, awg/write

Because it is a plain ROS client, it can run on the Pi OR on any other machine
on the same ROS graph -- that is the whole point of the backend/UI split.

Recording happens HERE, client-side: the subscribed JPEG stream is written to
MP4 in THIS app's own ./recordings folder, so footage lives with whoever runs
the UI (Pi or laptop), and the Pi takes no recording/disk load.

Run (after sourcing ROS 2 and the backend's scopio_interfaces):
    python3 run_ui.py                 # serves http://0.0.0.0:8080
Env: SCOPIO_UI_PORT (8080), SCOPIO_UI_PASSWORD ("password"),
     SCOPIO_NAMESPACE ("/scopio").
"""

import os
import re
import json
import time
import math
import secrets
import threading
import subprocess
from functools import wraps

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import QoSProfile, DurabilityPolicy, HistoryPolicy

from sensor_msgs.msg import CompressedImage
from scopio_interfaces.msg import CameraState, StagePosition, AwgStatus, BeadArray, Calibration
from scopio_interfaces.srv import (
    SetCameraControls, SetFramerate, WhiteBalance, StageJog, CalibrationSet, AwgWrite,
)
from scopio_interfaces.action import Autofocus

from flask import (
    Flask, Response, jsonify, render_template_string, request, session,
    redirect, url_for, send_from_directory, abort,
)

from galvo_geometry import GalvoClient

# ========== Config ==========
HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(HERE, "frontend")
RECORDINGS_DIR = os.path.join(HERE, "recordings")
NS = os.environ.get("SCOPIO_NAMESPACE", "/scopio").rstrip("/")
PORT = int(os.environ.get("SCOPIO_UI_PORT", 8080))
PASSWORD = os.environ.get("SCOPIO_UI_PASSWORD", "password")
# Optional: pull the live view from a Pi-hosted MJPEG server instead of the ROS
# image topic (used when the camera can't run inside the ROS container -- see
# pi_camera_server.py / WINDOWS_CLIENT.md). e.g. http://10.42.0.1:8081/stream.mjpg
CAMERA_MJPEG_URL = os.environ.get("CAMERA_MJPEG_URL", "").strip()
NAN = float("nan")


def _derive_cam_ctrl_base():
    """The camera control API lives on the same host:port as the MJPEG stream,
    e.g. http://10.42.0.1:8081/stream.mjpg -> http://10.42.0.1:8081. When set, the
    UI drives the real Pi camera over HTTP instead of the (camera-less) ROS node."""
    if not CAMERA_MJPEG_URL:
        return None
    from urllib.parse import urlsplit
    p = urlsplit(CAMERA_MJPEG_URL)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None


CAM_CTRL_BASE = _derive_cam_ctrl_base()
_stream_fps = 0.0                        # measured fps of the ingested MJPEG stream


def _cam_http(path, method="GET", payload=None, timeout=8.0):
    """Call the Pi camera server's control API and return the parsed JSON."""
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        CAM_CTRL_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

MIN_FPS, MAX_FPS = 1, 120

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


class UiClient(Node):
    """The ROS half: caches subscribed topics, holds service clients."""

    def __init__(self):
        super().__init__("scopio_ui")
        self._lock = threading.Lock()
        self.jpeg = None
        self.camera_state = None
        self.stage = None
        self.awg = None
        self.beads = None
        self.calibration = None

        def t(name):
            return f"{NS}/{name}"

        self.create_subscription(CompressedImage, t("image/compressed"), self._on_image, 5)
        self.create_subscription(CameraState, t("camera/state"), self._on_camera, 5)
        self.create_subscription(StagePosition, t("stage/position"), self._on_stage, 5)
        self.create_subscription(AwgStatus, t("awg/status"), self._on_awg, 5)
        self.create_subscription(BeadArray, t("beads"), self._on_beads, 5)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(Calibration, t("calibration"), self._on_calibration, latched)

        self.cli_controls = self.create_client(SetCameraControls, t("camera/set_controls"))
        self.cli_framerate = self.create_client(SetFramerate, t("camera/set_framerate"))
        self.cli_wb = self.create_client(WhiteBalance, t("camera/white_balance"))
        self.cli_jog = self.create_client(StageJog, t("stage/jog"))
        self.cli_calib = self.create_client(CalibrationSet, t("calibration/set"))
        self.cli_awg = self.create_client(AwgWrite, t("awg/write"))
        self.act_autofocus = ActionClient(self, Autofocus, t("camera/autofocus"))

    # --- subscription callbacks ---
    def _on_image(self, m):
        with self._lock:
            self.jpeg = bytes(m.data)

    def _on_camera(self, m):
        with self._lock:
            self.camera_state = m

    def _on_stage(self, m):
        with self._lock:
            self.stage = m

    def _on_awg(self, m):
        with self._lock:
            self.awg = m

    def _on_beads(self, m):
        with self._lock:
            self.beads = m

    def _on_calibration(self, m):
        with self._lock:
            self.calibration = m

    # --- synchronous service call from a Flask thread ---
    def call(self, client, req, timeout=5.0):
        if not client.wait_for_service(timeout_sec=1.0):
            raise RuntimeError(f"service {client.srv_name} unavailable")
        future = client.call_async(req)
        done = threading.Event()
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(timeout):
            raise RuntimeError("service call timed out")
        return future.result()

    @staticmethod
    def wait_future(future, timeout):
        done = threading.Event()
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(timeout):
            raise RuntimeError("ROS future timed out")
        return future.result()

    def run_autofocus(self, z_range=2000, steps=15, settle_s=0.2):
        if not self.act_autofocus.wait_for_server(timeout_sec=3.0):
            raise RuntimeError("autofocus action unavailable")
        goal = Autofocus.Goal()
        goal.z_range = int(z_range)
        goal.steps = int(steps)
        goal.settle_s = float(settle_s)
        handle = self.wait_future(self.act_autofocus.send_goal_async(goal), 5.0)
        if not handle.accepted:
            raise RuntimeError("autofocus goal rejected")
        return self.wait_future(handle.get_result_async(), 180.0).result


# ========== Flask app ==========
app = Flask(__name__)
app.secret_key = os.environ.get("SCOPIO_UI_SESSION_SECRET") or secrets.token_hex(16)

node = None          # set in main()
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


def _mjpeg_ingest_loop(url):
    """Pull an external MJPEG stream (e.g. the Pi's pi_camera_server.py) and push
    each JPEG into node.jpeg -- exactly where ROS image frames would land. This
    makes the live view AND client-side recording work even when the ROS camera
    topic is empty (no camera inside the container). Frames are split by JPEG
    markers, so any MJPEG boundary format works."""
    global _stream_fps
    import urllib.request
    last_t = time.time()
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                buf = b""
                while True:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(b"\xff\xd8")            # JPEG SOI
                        end = buf.find(b"\xff\xd9", start + 2)   # JPEG EOI
                        if start < 0 or end < 0:
                            break
                        frame = buf[start:end + 2]
                        buf = buf[end + 2:]
                        with node._lock:
                            node.jpeg = frame
                        now = time.time()
                        dt = now - last_t
                        last_t = now
                        if dt > 0:
                            _stream_fps = 0.85 * _stream_fps + 0.15 * (1.0 / dt)
                    if len(buf) > 4_000_000:                     # guard runaway buffer
                        buf = buf[-1_000_000:]
        except Exception as e:
            if node:
                node.get_logger().warning(f"MJPEG ingest ({url}) failed: {e}; retrying in 2s")
            time.sleep(2)


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
    cs = node.camera_state if node else None
    if cs is None:
        return {"red_gain": 2.4, "green_gain": 1.0, "blue_gain": 2.5, "framerate": 30,
                "exposure": 20000, "analogue_gain": 1.0, "colour_gain": 1.0,
                "contrast": 1.0, "saturation": 1.0, "brightness": 0.0, "sharpness": 1.0}
    return {"red_gain": cs.red_gain, "green_gain": cs.green_gain, "blue_gain": cs.blue_gain,
            "framerate": cs.target_fps, "exposure": cs.exposure_us,
            "analogue_gain": cs.analogue_gain, "colour_gain": cs.colour_gain,
            "contrast": cs.contrast, "saturation": cs.saturation,
            "brightness": cs.brightness, "sharpness": cs.sharpness}


@app.route("/video_feed")
@login_required
def video_feed():
    def gen():
        while True:
            with node._lock:
                jpeg = node.jpeg
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
    connected = bool(node.stage and node.stage.connected)
    return jsonify({"controller_connected": connected, "steps": steps})


@app.route("/telemetry")
@login_required
def telemetry():
    st = node.stage
    cs = node.camera_state
    pos = {"x": st.x, "y": st.y, "z": st.z} if st else {"x": 0, "y": 0, "z": 0}
    stage_um = {"x": st.x_um, "y": st.y_um, "z": st.z_um} if st else {"x": 0, "y": 0, "z": 0}
    laser = galvo.state()
    laser["connected"] = bool(node.awg and node.awg.connected)
    laser["global_um"] = {k: round(v, 1) for k, v in galvo.global_um(stage_um).items()}
    fps = round(_stream_fps, 1) if CAMERA_MJPEG_URL else (cs.measured_fps if cs else 0.0)
    return jsonify({
        "position": pos,
        "fps": fps,
        "target_fps": cs.target_fps if cs else 0.0,
        "controller_connected": bool(st and st.connected),
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
        req = StageJog.Request(); req.dx, req.dy, req.dz = delta
        res = node.call(node.cli_jog, req)
        if not res.success:
            return res.message or "jog failed", 503
    except Exception as e:
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


# ---- camera ----
@app.route("/get_camera_controls")
@login_required
def get_camera_controls():
    if CAM_CTRL_BASE:
        try:
            return jsonify(_cam_http("/controls"))
        except Exception:
            pass          # fall back to defaults if the camera server is down
    return jsonify(_cam_dict())


@app.route("/set_camera_controls", methods=["POST"])
@login_required
def set_camera_controls():
    d = request.get_json() or {}
    if CAM_CTRL_BASE:
        try:
            _cam_http("/controls", "POST", d)
            return "OK"
        except Exception as e:
            return str(e), 503
    req = SetCameraControls.Request()
    for f in ("red_gain", "green_gain", "blue_gain", "colour_gain", "analogue_gain",
              "contrast", "saturation", "brightness", "sharpness"):
        setattr(req, f, float(d[f]) if f in d else NAN)
    try:
        node.call(node.cli_controls, req)
    except Exception as e:
        return str(e), 503
    return "OK"


@app.route("/set_framerate", methods=["POST"])
@login_required
def set_framerate():
    d = request.get_json() or {}
    fps = float(d.get("fps", 30))
    if CAM_CTRL_BASE:
        try:
            res = _cam_http("/controls", "POST", {"framerate": fps})
            return jsonify({"framerate": res.get("framerate", fps),
                            "exposure": res.get("exposure", 0),
                            "analogue_gain": res.get("analogue_gain", 1.0)})
        except Exception as e:
            return jsonify({"error": str(e)}), 503
    req = SetFramerate.Request(); req.fps = fps
    try:
        res = node.call(node.cli_framerate, req)
    except Exception as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"framerate": res.framerate, "exposure": res.exposure_us,
                    "analogue_gain": res.analogue_gain})


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
            if CAM_CTRL_BASE:
                res = _cam_http("/white_balance", "POST", {}, timeout=8.0)
                node.get_logger().info(f"white balance: {res}")
            else:
                node.call(node.cli_wb, WhiteBalance.Request(), timeout=12.0)
        except Exception as e:
            node.get_logger().warning(f"white balance failed: {e}")
        finally:
            _wb_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"message": "White balance started"})


@app.route("/calibration_status")
@login_required
def calibration_status():
    return jsonify({"running": _wb_running or _af_running})


def _focus_metric():
    """Sharpness of the current frame: variance of the Laplacian (higher = sharper).
    Uses the live frame the UI already has (ROS topic or ingested MJPEG)."""
    with node._lock:
        j = node.jpeg
    if not j:
        return 0.0
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def _jog_z(dz):
    """Relative Z jog over ROS (drives the real stepper). Returns True if applied."""
    req = StageJog.Request(); req.dx = 0; req.dy = 0; req.dz = int(dz)
    try:
        res = node.call(node.cli_jog, req)
        return bool(getattr(res, "success", True))
    except Exception as e:
        node.get_logger().warning(f"autofocus jog failed: {e}")
        return False


def _run_autofocus(z_range=2000, steps=15, settle_s=0.35):
    """Sweep Z over a range, measure sharpness at each step, return to the sharpest.
    Approaches from below so mechanical backlash is taken up in one direction."""
    half = int(z_range // 2)
    step = max(1, int(z_range // max(1, steps)))
    if not _jog_z(-half):
        raise RuntimeError("stage not responding (is the controller connected?)")
    time.sleep(settle_s)
    best_m, best_z = -1.0, None
    for i in range(steps + 1):
        time.sleep(settle_s)
        m = _focus_metric()
        z = node.stage.z if node.stage else i * step
        if m > best_m:
            best_m, best_z = m, z
        if i < steps:
            _jog_z(step)
    if best_z is not None:                       # return to the sharpest plane
        cur = node.stage.z if node.stage else 0
        _jog_z(best_z - cur)
    return best_m, best_z


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
            if CAM_CTRL_BASE:                     # UI-orchestrated: real frames + ROS stage
                metric, z = _run_autofocus()
                node.get_logger().info(f"autofocus done: best sharpness {metric:.0f} at z={z}")
            else:                                 # backend action (ROS camera present)
                res = node.run_autofocus()
                node.get_logger().info(f"autofocus: {res.message} (best_z={res.best_z})")
        except Exception as e:
            node.get_logger().warning(f"autofocus failed: {e}")
        finally:
            _af_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"message": "Autofocus started"})


# ---- calibration ----
@app.route("/get_calibration")
@login_required
def get_calibration():
    c = node.calibration
    if c is None or not c.has_um_per_px:
        return jsonify({"um_per_px": None})
    return jsonify({"um_per_px": c.um_per_px})


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
    req = CalibrationSet.Request()
    req.um_per_px = um / px
    req.steps_per_um_x = req.steps_per_um_y = req.steps_per_um_z = NAN
    try:
        node.call(node.cli_calib, req)
    except Exception as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"um_per_px": um / px, "ref_pixels": px, "ref_micrometres": um})


# ---- galvo (compose SCPI client-side, send via awg/write) ----
def _send_awg(cmds):
    sent = 0
    for c in cmds:
        res = node.call(node.cli_awg, AwgWrite.Request(command=c))
        if not res.success:
            raise RuntimeError(res.error or "awg/write failed")
        sent += 1
    return sent


@app.route("/galvo/move/<direction>", methods=["POST"])
@login_required
def galvo_move(direction):
    try:
        _send_awg(galvo.jog_cmds(direction))
    except Exception as e:
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
    st = node.stage
    stage_um = {"x": st.x_um, "y": st.y_um, "z": st.z_um} if st else {"x": 0, "y": 0, "z": 0}
    s = galvo.state()
    s["connected"] = bool(node.awg and node.awg.connected)
    s["global_um"] = {k: round(v, 1) for k, v in galvo.global_um(stage_um).items()}
    return s


# ========== Client-side recording ==========
def _next_index():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    nums = [int(m.group(1)) for n in os.listdir(RECORDINGS_DIR)
            if (m := re.match(r"recording_(\d+)_", n))]
    return (max(nums) + 1) if nums else 1


def _record_loop(path, fps, duration):
    """Write the subscribed JPEG stream to an MP4 until stopped / duration up."""
    import cv2
    import numpy as np
    writer = None
    interval = 1.0 / max(1.0, fps)
    start = time.time()
    try:
        while not _rec["stop"].is_set():
            if duration and time.time() - start >= duration:
                break
            with node._lock:
                jpeg = node.jpeg
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
    cs = node.camera_state
    fps = round(cs.measured_fps or cs.target_fps) if cs else 15
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
    global node
    rclpy.init()
    node = UiClient()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    if CAMERA_MJPEG_URL:
        node.get_logger().info(f"Ingesting camera from MJPEG stream {CAMERA_MJPEG_URL}")
        threading.Thread(target=_mjpeg_ingest_loop, args=(CAMERA_MJPEG_URL,), daemon=True).start()
    node.get_logger().info(f"SCOPIO UI on http://0.0.0.0:{PORT} (ROS namespace {NS})")
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
