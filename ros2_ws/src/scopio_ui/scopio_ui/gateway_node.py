"""ui_gateway - the SCOPIO web UI as a ROS 2 *client* (owns no hardware).

    browser  <--HTTP/MJPEG-->  ui_gateway (this node)  <--ROS-->  driver nodes
                                                       <--ROS-->  any other client

It subscribes to the driver topics and caches the latest value, calls the
driver services when the browser POSTs a command, and serves the existing rich
SCOPIO frontend (read straight from microscope/frontend/) so the UI behaves as
before -- only now the backend is ROS.

Two things it does itself, as a client, on whatever machine runs it:
  * RECORDING. The Pi camera node only streams; this gateway saves the
    subscribed frames to mp4 in its OWN local ``recordings/`` folder. So footage
    lives with whoever is watching, and the Pi takes no recording load.
  * GALVO GEOMETRY. The galvo node is a dumb SCPI passthrough; this gateway uses
    microscope/galvo_geometry.GalvoClient to turn UI jogs into SCPI strings and
    sends them via the ``awg/write`` service.

This is the reference client; an autonomous controller is just another such
client on the same graph.
"""

import os
import re
import time
import json
import threading

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, HistoryPolicy

from sensor_msgs.msg import CompressedImage
from scopio_interfaces.msg import AwgStatus, BeadArray, Calibration, CameraState, StagePosition
from scopio_interfaces.srv import (
    AwgWrite, CalibrationSet, MoveAbs, SetCameraControls, SetFramerate, StageJog, WhiteBalance,
)

from flask import (
    Flask, Response, jsonify, render_template_string, request, send_from_directory, abort,
)

from . import _repo

_repo.ensure_on_path()
try:
    from galvo_geometry import GalvoClient
except Exception:
    GalvoClient = None

# Screen-direction -> stage step deltas (camera mounted 90 deg to stage axes):
# screen up/down -> stage X, left/right -> stage Y, focus -> Z.
def _dir_delta(direction, steps):
    return {
        "up":        (steps["x"], 0, 0),
        "down":      (-steps["x"], 0, 0),
        "left":      (0, -steps["y"], 0),
        "right":     (0, steps["y"], 0),
        "page_up":   (0, 0, steps["z"]),
        "page_down": (0, 0, -steps["z"]),
    }.get(direction)


class UiGateway(Node):
    def __init__(self):
        super().__init__("ui_gateway")
        self.declare_parameter("http_port", 8080)
        self.declare_parameter("recordings_dir", "recordings")
        self.http_port = int(self.get_parameter("http_port").value)
        self.recordings_dir = self.get_parameter("recordings_dir").value

        self._lock = threading.Lock()
        self._jpeg = None
        self._camera = None      # CameraState dict
        self._stage = None       # StagePosition dict
        self._awg = None         # AwgStatus dict
        self._beads = None
        self._calibration = {"um_per_px": None, "steps_per_um": {"x": 1.0, "y": 1.0, "z": 1.0}}

        self.steps = {"x": 40, "y": 40, "z": 40}      # client-side jog step size
        self.galvo = GalvoClient() if GalvoClient else None

        # Recording (client-side)
        self._rec = None         # dict while recording, else None
        self._rec_writer = None
        self._busy = False       # autofocus/white-balance in progress (for UI poll)

        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             history=HistoryPolicy.KEEP_LAST)
        self.create_subscription(CompressedImage, "image/compressed", self._on_image, 5)
        self.create_subscription(CameraState, "camera/state", self._on_camera, 5)
        self.create_subscription(StagePosition, "stage/position", self._on_stage, 5)
        self.create_subscription(AwgStatus, "awg/status", self._on_awg, 5)
        self.create_subscription(BeadArray, "beads", self._on_beads, 5)
        self.create_subscription(Calibration, "calibration", self._on_calibration, latched)

        self.cli_jog = self.create_client(StageJog, "stage/jog")
        self.cli_move_abs = self.create_client(MoveAbs, "stage/move_abs")
        self.cli_cam = self.create_client(SetCameraControls, "camera/set_controls")
        self.cli_fps = self.create_client(SetFramerate, "camera/set_framerate")
        self.cli_wb = self.create_client(WhiteBalance, "camera/white_balance")
        self.cli_awg = self.create_client(AwgWrite, "awg/write")
        self.cli_calib = self.create_client(CalibrationSet, "calibration/set")

        self._app = self._build_flask_app()
        self.get_logger().info(f"UI gateway on http://0.0.0.0:{self.http_port}")

    # ================= ROS callbacks ================= #
    def _on_image(self, msg):
        data = bytes(msg.data)
        with self._lock:
            self._jpeg = data
        if self._rec is not None:
            self._write_rec_frame(data)

    def _on_camera(self, msg):
        with self._lock:
            self._camera = {
                "connected": msg.connected, "framerate": msg.target_fps,
                "measured_fps": msg.measured_fps, "exposure": msg.exposure_us,
                "analogue_gain": msg.analogue_gain, "red_gain": msg.red_gain,
                "green_gain": msg.green_gain, "blue_gain": msg.blue_gain,
                "colour_gain": msg.colour_gain, "contrast": msg.contrast,
                "saturation": msg.saturation, "brightness": msg.brightness,
                "sharpness": msg.sharpness, "width": msg.width, "height": msg.height,
            }

    def _on_stage(self, msg):
        with self._lock:
            self._stage = {"connected": msg.connected, "x": msg.x, "y": msg.y, "z": msg.z,
                           "x_um": msg.x_um, "y_um": msg.y_um, "z_um": msg.z_um}

    def _on_awg(self, msg):
        with self._lock:
            self._awg = {"connected": msg.connected, "idn": msg.idn,
                         "last_command": msg.last_command, "last_error": msg.last_error}

    def _on_beads(self, msg):
        with self._lock:
            self._beads = {"count": msg.count, "clump_count": msg.clump_count}

    def _on_calibration(self, msg):
        with self._lock:
            self._calibration = {
                "um_per_px": msg.um_per_px if msg.has_um_per_px else None,
                "steps_per_um": {"x": msg.steps_per_um_x, "y": msg.steps_per_um_y,
                                 "z": msg.steps_per_um_z}}

    # ================= ROS call helper ================= #
    def _call(self, client, req, timeout=4.0):
        if not client.wait_for_service(timeout_sec=1.0):
            raise RuntimeError(f"service {client.srv_name} unavailable")
        future = client.call_async(req)
        done = threading.Event()
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(timeout):
            raise RuntimeError("service call timed out")
        return future.result()

    def _awg_send(self, cmds):
        """Send a list of SCPI strings via awg/write; raise on first failure."""
        for c in cmds:
            req = AwgWrite.Request(); req.command = c
            res = self._call(self.cli_awg, req)
            if not res.success:
                raise RuntimeError(res.error or "awg write failed")

    # ================= Recording (client-side) ================= #
    def _write_rec_frame(self, jpeg):
        try:
            import cv2
            import numpy as np
            bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                return
            if self._rec_writer is None:
                h, w = bgr.shape[:2]
                fps = (self._camera or {}).get("measured_fps") or 15.0
                fps = max(1.0, float(fps))
                os.makedirs(self.recordings_dir, exist_ok=True)
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                self._rec_writer = cv2.VideoWriter(self._rec["path"], fourcc, fps, (w, h))
                self._rec["fps"] = round(fps, 1)
            self._rec_writer.write(bgr)
            self._rec["frames"] += 1
        except Exception as e:
            self.get_logger().warning(f"record frame failed: {e}")

    def _start_recording(self, duration_s):
        os.makedirs(self.recordings_dir, exist_ok=True)
        idx = len([n for n in os.listdir(self.recordings_dir)
                   if n.lower().endswith(".mp4")]) + 1
        fps_tag = int((self._camera or {}).get("measured_fps") or 0)
        dur_tag = f"{int(duration_s)}s" if duration_s else "inf"
        name = f"recording_{idx}_{fps_tag}fps_{dur_tag}.mp4"
        path = os.path.join(self.recordings_dir, name)
        self._rec = {"name": name, "path": path, "frames": 0, "fps": 0.0,
                     "started": time.time(), "duration": duration_s}
        self._rec_writer = None
        if duration_s:
            threading.Timer(duration_s, self._stop_recording).start()
        return name

    def _stop_recording(self):
        if self._rec is None:
            return None
        rec, self._rec = self._rec, None
        writer, self._rec_writer = self._rec_writer, None
        if writer is not None:
            writer.release()
        elapsed = max(1, int(round(time.time() - rec["started"])))
        # rename to actual length and write a metadata sidecar (fps/duration)
        new_path = re.sub(r"_(\d+s|inf)\.mp4$", f"_{elapsed}s.mp4", rec["path"])
        try:
            if os.path.exists(rec["path"]) and new_path != rec["path"]:
                os.rename(rec["path"], new_path)
        except OSError:
            new_path = rec["path"]
        meta = {"fps": rec["fps"], "duration": elapsed, "frames": rec["frames"]}
        try:
            with open(new_path + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except OSError:
            pass
        return os.path.basename(new_path)

    # ================= Autofocus (client-side routine) ================= #
    def _autofocus(self, z_range=2000, n=15, settle=0.25):
        self._busy = True
        try:
            def metric():
                with self._lock:
                    return len(self._jpeg) if self._jpeg else 0
            best_z, best = 0, -1
            start = (self._stage or {}).get("z", 0)
            lo = -z_range
            # move to bottom of sweep
            self._jog(0, 0, lo)
            step = (2 * z_range) // max(1, n - 1)
            z = lo
            for _ in range(n):
                time.sleep(settle)
                m = metric()
                if m > best:
                    best, best_z = m, z
                self._jog(0, 0, step); z += step
            # return to best (approach from below for backlash consistency)
            cur = (self._stage or {}).get("z", 0) - start
            self._jog(0, 0, best_z - cur)
        except Exception as e:
            self.get_logger().warning(f"autofocus failed: {e}")
        finally:
            self._busy = False

    def _jog(self, dx, dy, dz):
        req = StageJog.Request(); req.dx, req.dy, req.dz = int(dx), int(dy), int(dz)
        return self._call(self.cli_jog, req)

    # ================= Flask ================= #
    def _build_flask_app(self):
        app = Flask(__name__)
        frontend = os.path.join(_repo.repo_root() or "", "microscope", "frontend")

        def read(name):
            with open(os.path.join(frontend, name), encoding="utf-8") as f:
                return f.read()

        @app.route("/")
        def index():
            cam = self._camera or {"red_gain": 2.4, "green_gain": 1.0, "blue_gain": 2.5,
                                   "framerate": 30, "exposure": 20000, "analogue_gain": 1.0,
                                   "colour_gain": 1.0, "contrast": 1.0, "saturation": 1.0,
                                   "brightness": 0.0, "sharpness": 1.0}
            doc = (read("index.html").replace("__STYLE__", read("style.css"))
                   .replace("__SCRIPT__", read("app.js")).replace("__LOGO__", read("logo.svg")))
            return render_template_string(doc, steps=self.steps, record_duration=600,
                                          cam=cam, min_fps=1, max_fps=120)

        @app.route("/video_feed")
        def video_feed():
            def gen():
                while True:
                    with self._lock:
                        jpeg = self._jpeg
                    if jpeg is None:
                        time.sleep(0.03); continue
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                    time.sleep(1 / 15.0)
            return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

        @app.route("/status")
        def status():
            st = self._stage or {}
            return jsonify({"controller_connected": bool(st.get("connected")),
                            "controller_error": None, "steps": self.steps})

        @app.route("/telemetry")
        def telemetry():
            st = self._stage or {}
            cam = self._camera or {}
            laser = self.galvo.state() if self.galvo else {}
            laser["connected"] = bool((self._awg or {}).get("connected"))
            return jsonify({"position": {"x": st.get("x", 0), "y": st.get("y", 0), "z": st.get("z", 0)},
                            "fps": cam.get("measured_fps", 0.0),
                            "target_fps": cam.get("framerate", 0.0),
                            "controller_connected": bool(st.get("connected")), "laser": laser})

        # ---- stage ----
        @app.route("/move/<direction>")
        def move(direction):
            delta = _dir_delta(direction, self.steps)
            if delta is None:
                return "Unknown direction", 404
            try:
                self._jog(*delta); return "OK", 200
            except Exception as e:
                return str(e), 503

        @app.route("/adjust/<axis>/<op>")
        def adjust(axis, op):
            if axis not in self.steps:
                return "Unknown axis", 404
            self.steps[axis] = (self.steps[axis] + 5 if op == "inc"
                                else max(1, self.steps[axis] - 5))
            return str(self.steps[axis]), 200

        @app.route("/set_step/<axis>/<int:value>")
        def set_step(axis, value):
            if axis not in self.steps:
                return "Unknown axis", 404
            self.steps[axis] = max(1, int(value))
            return str(self.steps[axis]), 200

        # ---- camera ----
        @app.route("/get_camera_controls")
        def get_cam():
            return jsonify(self._camera or {})

        @app.route("/set_camera_controls", methods=["POST"])
        def set_cam():
            d = request.get_json(force=True) or {}
            req = SetCameraControls.Request()
            for k in ("red_gain", "green_gain", "blue_gain", "colour_gain", "analogue_gain",
                      "contrast", "saturation", "brightness", "sharpness"):
                setattr(req, k, float(d[k]) if k in d else float("nan"))
            try:
                self._call(self.cli_cam, req); return "OK"
            except Exception as e:
                return str(e), 503

        @app.route("/set_framerate", methods=["POST"])
        def set_fps():
            d = request.get_json(force=True) or {}
            req = SetFramerate.Request(); req.fps = float(d.get("fps", 30))
            try:
                res = self._call(self.cli_fps, req)
                return jsonify({"framerate": res.framerate, "exposure": res.exposure_us,
                                "analogue_gain": res.analogue_gain})
            except Exception as e:
                return str(e), 503

        @app.route("/white_balance", methods=["POST"])
        def white_balance():
            def run():
                self._busy = True
                try:
                    self._call(self.cli_wb, WhiteBalance.Request(), timeout=12.0)
                except Exception as e:
                    self.get_logger().warning(f"white balance failed: {e}")
                finally:
                    self._busy = False
            threading.Thread(target=run, daemon=True).start()
            return jsonify({"message": "White balance started"})

        @app.route("/autofocus", methods=["POST"])
        def autofocus():
            if not (self._stage or {}).get("connected"):
                return jsonify({"error": "Sangaboard not connected"}), 503
            if self._busy:
                return jsonify({"error": "Calibration already in progress"}), 409
            threading.Thread(target=self._autofocus, daemon=True).start()
            return jsonify({"message": "Autofocus started"})

        @app.route("/calibration_status")
        def calibration_status():
            return jsonify({"running": self._busy})

        # ---- galvo (SCPI passthrough via client geometry) ----
        @app.route("/galvo/status")
        def galvo_status():
            if self.galvo is None:
                return jsonify({"connected": False})
            s = self.galvo.state(); s["connected"] = bool((self._awg or {}).get("connected"))
            return jsonify(s)

        @app.route("/galvo/move/<direction>", methods=["POST"])
        def galvo_move(direction):
            if self.galvo is None:
                return jsonify({"error": "galvo helper unavailable"}), 503
            try:
                self._awg_send(self.galvo.jog_cmds(direction))
                return jsonify(self.galvo.state())
            except Exception as e:
                return jsonify({"error": str(e)}), 503

        @app.route("/galvo/zero", methods=["POST"])
        def galvo_zero():
            if self.galvo is None:
                return jsonify({"error": "galvo helper unavailable"}), 503
            self.galvo.set_home()
            return jsonify(self.galvo.state())

        @app.route("/galvo/set_jog", methods=["POST"])
        def galvo_set_jog():
            d = request.get_json(force=True) or {}
            self.galvo.set_jog_volts(d.get("volts", 0.05))
            return jsonify({"jog_volts": self.galvo.jog_volts})

        @app.route("/galvo/output", methods=["POST"])
        def galvo_output():
            d = request.get_json(force=True) or {}
            try:
                self._awg_send(self.galvo.output_cmds(bool(d.get("on", True))))
                return jsonify({"on": bool(d.get("on", True))})
            except Exception as e:
                return jsonify({"error": str(e)}), 503

        # ---- calibration ----
        @app.route("/get_calibration")
        def get_calibration():
            c = self._calibration
            return jsonify({"um_per_px": c["um_per_px"]})

        @app.route("/set_calibration", methods=["POST"])
        def set_calibration():
            d = request.get_json(force=True) or {}
            try:
                px = float(d["pixels"]); um = float(d["micrometres"])
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "Need pixels and micrometres"}), 400
            if px <= 0 or um <= 0:
                return jsonify({"error": "Values must be positive"}), 400
            req = CalibrationSet.Request()
            req.um_per_px = um / px
            req.steps_per_um_x = req.steps_per_um_y = req.steps_per_um_z = float("nan")
            try:
                self._call(self.cli_calib, req)
                return jsonify({"um_per_px": um / px, "ref_pixels": px, "ref_micrometres": um})
            except Exception as e:
                return jsonify({"error": str(e)}), 503

        # ---- recording (client-side, local folder) ----
        @app.route("/start_recording", methods=["POST"])
        def start_recording():
            if self._rec is not None:
                return jsonify({"error": "Already recording"}), 409
            # duration None/0 => infinite. The UI sets it via /set_recording_setting.
            dur = getattr(self, "_rec_duration", 600)
            name = self._start_recording(dur)
            return jsonify({"filename": name, "duration": dur})

        @app.route("/set_recording_setting", methods=["POST"])
        def set_recording_setting():
            d = request.get_json(force=True) or {}
            if d.get("setting") == "duration":
                v = d.get("value")
                self._rec_duration = None if not v else max(1, int(v))
                return "OK"
            return "Invalid setting", 400

        @app.route("/stop_recording", methods=["POST"])
        def stop_recording():
            if self._rec is None:
                return jsonify({"error": "Not recording"}), 400
            name = self._stop_recording()
            return jsonify({"message": "stopped", "filename": name})

        @app.route("/recording_status")
        def recording_status():
            if self._rec is None:
                return jsonify({"recording": False, "duration": None, "remaining": None})
            dur = self._rec["duration"]
            remaining = None
            if dur:
                remaining = max(0, int(dur - (time.time() - self._rec["started"])))
            return jsonify({"recording": True, "duration": dur, "remaining": remaining})

        @app.route("/recordings")
        def recordings():
            os.makedirs(self.recordings_dir, exist_ok=True)
            items = []
            for n in os.listdir(self.recordings_dir):
                if not n.lower().endswith(".mp4"):
                    continue
                path = os.path.join(self.recordings_dir, n)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                meta = {}
                try:
                    with open(path + ".json", encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, json.JSONDecodeError):
                    pass
                items.append({"name": n, "duration": meta.get("duration"),
                              "fps": meta.get("fps"), "size": st.st_size, "mtime": st.st_mtime})
            items.sort(key=lambda x: x["mtime"], reverse=True)
            return jsonify(items)

        def _safe(name):
            return name if name and name == os.path.basename(name) and name.lower().endswith(".mp4") else None

        @app.route("/recordings/file/<path:name>")
        def recording_file(name):
            if _safe(name) is None:
                abort(404)
            return send_from_directory(self.recordings_dir, name, conditional=True)

        @app.route("/recordings/delete", methods=["POST"])
        def delete_recording():
            name = _safe((request.get_json(force=True) or {}).get("name", ""))
            if name is None:
                return jsonify({"error": "Invalid name"}), 400
            try:
                os.remove(os.path.join(self.recordings_dir, name))
                if os.path.exists(os.path.join(self.recordings_dir, name + ".json")):
                    os.remove(os.path.join(self.recordings_dir, name + ".json"))
            except OSError as e:
                return jsonify({"error": str(e)}), 404
            return jsonify({"message": "deleted"})

        @app.route("/recordings/rename", methods=["POST"])
        def rename_recording():
            d = request.get_json(force=True) or {}
            name = _safe(d.get("name", ""))
            if name is None:
                return jsonify({"error": "Invalid name"}), 400
            base = re.sub(r"[^A-Za-z0-9 _.\-]", "", os.path.splitext(d.get("new_name", ""))[0]).strip()
            if not base:
                return jsonify({"error": "Empty name"}), 400
            new = base + ".mp4"
            src = os.path.join(self.recordings_dir, name)
            dst = os.path.join(self.recordings_dir, new)
            if os.path.exists(dst) and dst != src:
                return jsonify({"error": "Name exists"}), 409
            try:
                os.rename(src, dst)
                if os.path.exists(src + ".json"):
                    os.rename(src + ".json", dst + ".json")
            except OSError as e:
                return jsonify({"error": str(e)}), 400
            return jsonify({"name": new})

        return app

    def run_http(self):
        self._app.run(host="0.0.0.0", port=self.http_port,
                      debug=False, use_reloader=False, threaded=True)


def main(args=None):
    rclpy.init(args=args)
    node = UiGateway()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()
    try:
        node.run_http()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
