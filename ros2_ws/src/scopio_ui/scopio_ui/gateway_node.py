"""ui_gateway - the SCOPIO web UI as a ROS 2 *client*.

This node owns NO hardware. It is the bridge that lets the human UI and the
external decision computer coexist on the same ROS graph:

    browser  <--HTTP/MJPEG-->  ui_gateway (this node)  <--ROS-->  driver nodes
                                                       <--ROS-->  decision computer

It does three ROS things, which are the three ways ROS nodes talk:
  1. SUBSCRIBES to topics (image, stage position, laser state, beads, recording)
     and caches the latest value of each -> served to the browser as telemetry.
  2. Calls SERVICES (zero tweezers, set laser, toggle tracker, set recording)
     when the browser POSTs a command.
  3. Sends ACTION goals (run galvo waveform, move stage path) for long jobs.

It runs a tiny Flask server in the same process. ROS spins in a background
thread; Flask request handlers talk to ROS through call_async + a wait helper.

Why this matters: the old Flask app drove the camera/stage/galvo *directly*, so
it could not run at the same time as the ROS driver nodes (one owner per
device). This gateway drives nothing directly -- it only asks ROS -- so it runs
happily alongside the drivers. That is the resolution of the "Flask vs ROS"
question: ROS owns the hardware; the UI becomes one more ROS client.
"""

import os
import threading

import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from ament_index_python.packages import get_package_share_directory

from std_srvs.srv import SetBool
from sensor_msgs.msg import CompressedImage
from scopio_interfaces.msg import BeadArray, LaserState, RecordingStatus, StagePosition
from scopio_interfaces.srv import SetLaser, SetRecording, ZeroTweezers
from scopio_interfaces.action import MoveStagePath, RunGalvoWaveform

from flask import Flask, Response, jsonify, request, send_from_directory


class UiGateway(Node):
    def __init__(self):
        super().__init__("ui_gateway")
        self.declare_parameter("http_port", 8080)
        self.http_port = int(self.get_parameter("http_port").value)

        # --- cached latest state (written by ROS callbacks, read by Flask) ---
        self._lock = threading.Lock()
        self._jpeg = None
        self._stage = None
        self._laser = None
        self._beads = None
        self._recording = None

        # --- 1) SUBSCRIPTIONS: cache the latest message on each topic ---
        # Topic names are relative; the node is launched in the /scopio
        # namespace, so "image/compressed" resolves to /scopio/image/compressed.
        self.create_subscription(CompressedImage, "image/compressed", self._on_image, 5)
        self.create_subscription(StagePosition, "stage/position", self._on_stage, 5)
        self.create_subscription(LaserState, "laser/state", self._on_laser, 5)
        self.create_subscription(BeadArray, "beads", self._on_beads, 5)
        self.create_subscription(RecordingStatus, "recording/status", self._on_recording, 5)

        # --- 2) SERVICE CLIENTS ---
        self.cli_zero = self.create_client(ZeroTweezers, "tweezers/zero")
        self.cli_set_laser = self.create_client(SetLaser, "laser/set")
        self.cli_tracker = self.create_client(SetBool, "tracker/set_active")
        self.cli_recording = self.create_client(SetRecording, "recording/set")

        # --- 3) ACTION CLIENTS ---
        self.act_waveform = ActionClient(self, RunGalvoWaveform, "galvo/run_waveform")
        self.act_move_path = ActionClient(self, MoveStagePath, "stage/move_path")

        self._app = self._build_flask_app()
        self.get_logger().info(f"UI gateway serving on http://0.0.0.0:{self.http_port}")

    # ================== ROS subscription callbacks ================== #
    def _on_image(self, msg):
        with self._lock:
            self._jpeg = bytes(msg.data)

    def _on_stage(self, msg):
        with self._lock:
            self._stage = {"connected": msg.connected, "x": msg.x, "y": msg.y, "z": msg.z}

    def _on_laser(self, msg):
        with self._lock:
            self._laser = {
                "connected": msg.connected,
                "vx": round(msg.vx, 4), "vy": round(msg.vy, 4),
                "home_vx": round(msg.home_vx, 4), "home_vy": round(msg.home_vy, 4),
                "image_x": round(msg.image_x, 1), "image_y": round(msg.image_y, 1),
                "global_x": round(msg.global_x, 1), "global_y": round(msg.global_y, 1),
            }

    def _on_beads(self, msg):
        with self._lock:
            self._beads = {"count": msg.count, "clump_count": msg.clump_count}

    def _on_recording(self, msg):
        with self._lock:
            self._recording = {
                "recording": msg.recording, "remaining_s": round(msg.remaining_s, 1),
                "filename": msg.filename}

    # ================== ROS call helpers ================== #
    def _call(self, client, request_msg, timeout=4.0):
        """Synchronously call a service from a Flask thread. The executor is
        spinning in another thread, so we fire call_async and block on the
        future via an Event that the future's done-callback sets."""
        if not client.wait_for_service(timeout_sec=1.0):
            raise RuntimeError(f"service {client.srv_name} unavailable")
        future = client.call_async(request_msg)
        done = threading.Event()
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(timeout):
            raise RuntimeError("service call timed out")
        return future.result()

    def _send_goal(self, action_client, goal_msg, timeout=2.0):
        """Fire-and-forget an action goal (the UI doesn't block on long jobs)."""
        if not action_client.wait_for_server(timeout_sec=timeout):
            raise RuntimeError("action server unavailable")
        action_client.send_goal_async(goal_msg)   # result tracked by the graph

    # ================== Flask app ================== #
    def _build_flask_app(self):
        app = Flask(__name__)
        frontend = os.path.join(get_package_share_directory("scopio_ui"), "frontend")

        @app.route("/")
        def index():
            return send_from_directory(frontend, "index.html")

        @app.route("/app.js")
        def appjs():
            return send_from_directory(frontend, "app.js")

        @app.route("/video_feed")
        def video_feed():
            import time

            def gen():
                while True:
                    with self._lock:
                        jpeg = self._jpeg
                    if jpeg is None:
                        time.sleep(0.05)
                        continue
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + jpeg + b"\r\n")
                    time.sleep(1 / 15.0)
            return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

        @app.route("/api/state")
        def state():
            with self._lock:
                return jsonify({
                    "stage": self._stage, "laser": self._laser,
                    "beads": self._beads, "recording": self._recording})

        @app.route("/api/tweezers/zero", methods=["POST"])
        def zero():
            res = self._call(self.cli_zero, ZeroTweezers.Request())
            return jsonify({"success": res.success,
                            "home_vx": res.home_vx, "home_vy": res.home_vy})

        @app.route("/api/laser/set", methods=["POST"])
        def set_laser():
            d = request.get_json(force=True) or {}
            req = SetLaser.Request()
            req.vx = float(d.get("vx", 0.0)); req.vy = float(d.get("vy", 0.0))
            req.relative = bool(d.get("relative", True))
            res = self._call(self.cli_set_laser, req)
            return jsonify({"success": res.success, "vx": res.vx, "vy": res.vy})

        @app.route("/api/tracker", methods=["POST"])
        def tracker():
            d = request.get_json(force=True) or {}
            req = SetBool.Request(); req.data = bool(d.get("active", False))
            res = self._call(self.cli_tracker, req)
            return jsonify({"success": res.success, "message": res.message})

        @app.route("/api/recording", methods=["POST"])
        def recording():
            d = request.get_json(force=True) or {}
            req = SetRecording.Request()
            req.start = bool(d.get("start", False))
            req.duration_s = float(d.get("duration_s", 0.0))
            res = self._call(self.cli_recording, req)
            return jsonify({"success": res.success, "filename": res.filename,
                            "message": res.message})

        @app.route("/api/galvo/waveform", methods=["POST"])
        def waveform():
            d = request.get_json(force=True) or {}
            g = RunGalvoWaveform.Goal()
            g.shape = str(d.get("shape", "circle"))
            g.x_freq_hz = float(d.get("x_freq_hz", 2.0))
            g.y_freq_hz = float(d.get("y_freq_hz", 2.0))
            g.amplitude_vpp = float(d.get("amplitude_vpp", 1.0))
            g.x_phase_deg = float(d.get("x_phase_deg", 0.0))
            g.y_phase_deg = float(d.get("y_phase_deg", 90.0))
            g.duration_s = float(d.get("duration_s", 5.0))
            self._send_goal(self.act_waveform, g)
            return jsonify({"accepted": True})

        return app

    def run_http(self):
        # threaded=True so the MJPEG stream doesn't block API calls.
        self._app.run(host="0.0.0.0", port=self.http_port,
                      debug=False, use_reloader=False, threaded=True)


def main(args=None):
    rclpy.init(args=args)
    node = UiGateway()

    # ROS spins in a background thread; Flask owns the main thread.
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
