"""camera_node - the graph's camera surface, backed by the camera server.

A PURE SENSOR + control surface. It publishes the live view as a
CompressedImage and exposes the camera's manual controls, the single
frame-rate knob (which auto-derives exposure/gain), and a one-shot hardware
white balance.

It does not own the sensor. picamera2/libcamera ship from Raspberry Pi OS, not
Ubuntu, so they cannot run in this container: the camera belongs to the
loopback camera server (camera_server/, its own compose service or systemd
unit), and this node is that server's client in the graph. It
  * ingests the server's MJPEG stream (CAMERA_URL, default
    http://127.0.0.1:8081) and republishes the JPEG frames on image/compressed
    -- no re-encode -- but ONLY while something wants them (a subscriber, or a
    running autofocus). Nothing subscribes by default (the gateway serves video
    as MJPEG, never over this topic), and scanning every frame of a 200 fps
    stream for nobody was pure Pi CPU;
  * forwards the camera services to the server's HTTP API, keeping the
    exposure-budget math here;
  * reads the server's live settings back every couple of seconds, so
    camera/state stays true when a client changes them through the gateway's
    /camera/controls instead of the ROS services (both paths exist, and the
    server is the truth);
  * runs the Autofocus action on the ingested frames.

(It used to have a second, "native" mode that opened picamera2 itself. That
could only run outside the container, so it never ran and was never tested --
it was removed rather than kept as a path that silently rots.)

It deliberately does NOT record, and NOTHING in the backend analyses the
frames. Both are *client* concerns: the UI (or any program) takes the video
stream, saves to its own local folder and runs its own detection/tracking
there, so the Pi takes no extra recording, disk or CPU load. The only pixel
work here is the autofocus sharpness metric, which is a hardware control loop,
not scene analysis.

Topics / services / actions (under /scopio):
  pub     image/compressed     sensor_msgs/CompressedImage    (JPEG live view)
  pub     camera/state         scopio_interfaces/CameraState  (settings + real fps)
  srv     camera/set_controls  scopio_interfaces/SetCameraControls
  srv     camera/set_framerate scopio_interfaces/SetFramerate
  srv     camera/white_balance scopio_interfaces/WhiteBalance
  action  camera/autofocus     scopio_interfaces/Autofocus

Degrades gracefully: with no camera server it reports connected=false and keeps
retrying, so the rest of the graph still comes up.
"""

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from sensor_msgs.msg import CompressedImage
from scopio_interfaces.msg import CameraState
from scopio_interfaces.srv import SetCameraControls, SetFramerate, WhiteBalance, StageJog
from scopio_interfaces.action import Autofocus

MIN_FPS, MAX_FPS = 1.0, 120.0
AF_BACKLASH = 256          # steps; every Z approached from below by this much
SERVER_POLL_S = 2.0        # how often camera/state re-reads the camera server

SOI, EOI = b"\xff\xd8", b"\xff\xd9"   # JPEG frame markers (MJPEG splitting)


class CameraNode(Node):
    def __init__(self):
        super().__init__("camera_node")
        # width/height are only what camera/state reports until the server
        # answers: the real size comes from its sensor mode (_refresh_from_server).
        self.declare_parameter("width", 640)
        self.declare_parameter("height", 480)
        self.declare_parameter("framerate", 30.0)
        self.declare_parameter("publish_fps", 15.0)

        self.width = int(self.get_parameter("width").value)
        self.height = int(self.get_parameter("height").value)

        # Live control state -- what camera/state reports and what gets pushed
        # to the camera server. green_gain is kept for the frozen interface
        # (SetCameraControls / CameraState carry it) but has no effect: the ISP
        # has red and blue gains only, and frames pass through unmodified.
        self.cam = {
            "red_gain": 2.4, "green_gain": 1.0, "blue_gain": 2.5,
            "framerate": float(self.get_parameter("framerate").value),
            "exposure": 20000, "analogue_gain": 1.0, "colour_gain": 1.0,
            "contrast": 1.0, "saturation": 1.0, "brightness": 0.0, "sharpness": 1.0,
        }
        # Brightness target: exposure_us * analogue_gain held constant as fps changes.
        self.exposure_budget = self.cam["exposure"] * self.cam["analogue_gain"]

        self._np = None
        self._cv2 = None

        # Camera server (loopback HTTP) state.
        self.bridge_url = None
        self._latest_jpeg = None
        self._latest_jpeg_at = 0.0        # monotonic; also the frame's identity
        self._published_at = 0.0
        self._bridge_fps = 0.0
        self._bridge_stop = threading.Event()
        self._af_active = False           # autofocus needs frames, subscribed or not
        self._server_ok_at = 0.0          # monotonic time of the last good GET /controls
        self._server_frames = None        # (encoder frame count, monotonic) at that GET
        self._server_fps = 0.0            # encoder rate derived from successive GETs
        self._pushed_controls = False     # see _sync_bridge_controls
        self._push_retry_at = 0.0
        self._refresh_at = 0.0            # see _refresh_from_server

        self.image_pub = self.create_publisher(CompressedImage, "image/compressed", 5)
        self.state_pub = self.create_publisher(CameraState, "camera/state", 5)

        cb = ReentrantCallbackGroup()
        self.create_service(SetCameraControls, "camera/set_controls", self._on_set_controls, callback_group=cb)
        self.create_service(SetFramerate, "camera/set_framerate", self._on_set_framerate, callback_group=cb)
        self.create_service(WhiteBalance, "camera/white_balance", self._on_white_balance, callback_group=cb)

        # Autofocus coordinates this camera's focus metric with the stage's Z.
        self._af_cb = ReentrantCallbackGroup()
        self.cli_jog = self.create_client(StageJog, "stage/jog", callback_group=self._af_cb)
        self._af_server = ActionServer(
            self, Autofocus, "camera/autofocus",
            execute_callback=self._execute_autofocus,
            goal_callback=lambda g: GoalResponse.ACCEPT,
            cancel_callback=lambda c: CancelResponse.ACCEPT,
            callback_group=self._af_cb)

        self._start_bridge()

        publish_fps = max(1.0, float(self.get_parameter("publish_fps").value))
        self.create_timer(1.0 / publish_fps, self._publish_frame, callback_group=cb)
        self.create_timer(0.5, self._publish_state, callback_group=cb)

    @property
    def connected(self):
        if self.bridge_url is None:
            return False
        # Connected while the server answers GET /controls with a 200 (it 503s
        # until the sensor is open), or while ingested frames arrive. Frames
        # alone do not prove it: nothing is ingested unless wanted.
        now = time.monotonic()
        return (now - self._server_ok_at < 3 * SERVER_POLL_S
                or now - self._latest_jpeg_at < 5.0)

    def _want_frames(self):
        """Ingest only while somebody will use the frames."""
        return self._af_active or self.image_pub.get_subscription_count() > 0

    # ------------------------------------------------------------------ #
    #  The camera server link
    # ------------------------------------------------------------------ #
    def _start_bridge(self):
        url = os.environ.get("CAMERA_URL", "http://127.0.0.1:8081").rstrip("/")
        if not url:
            self.get_logger().warning("No CAMERA_URL; camera idling.")
            return
        try:
            import cv2
            import numpy as np
            self._cv2, self._np = cv2, np
        except Exception as e:
            self.get_logger().warning(f"cv2/numpy unavailable ({e}); camera idling.")
            return
        self.bridge_url = url
        threading.Thread(target=self._bridge_ingest_loop, daemon=True,
                         name="camera-bridge").start()
        self.get_logger().info(f"Camera server at {url}")

    def _bridge_ingest_loop(self):
        """Pull the camera server's MJPEG stream while it is wanted; keep the
        newest JPEG. Idles (no connection at all) while nobody wants frames, and
        reconnects with backoff so a camera restart heals automatically."""
        frames, t0 = 0, time.monotonic()
        while not self._bridge_stop.is_set():
            if not self._want_frames():
                self._bridge_fps = 0.0
                self._bridge_stop.wait(0.5)
                continue
            try:
                # `with`: without it a reconnect loop leaks one socket per retry,
                # and a camera that is down for a while runs the node out of fds.
                with urllib.request.urlopen(f"{self.bridge_url}/stream.mjpg",
                                            timeout=10) as resp:
                    buf = b""
                    frames, t0 = 0, time.monotonic()
                    checked = t0
                    while not self._bridge_stop.is_set():
                        # Twice a second, not per chunk: a fast stream is
                        # hundreds of chunks/s, each an rclpy graph query.
                        if time.monotonic() - checked >= 0.5:
                            checked = time.monotonic()
                            if not self._want_frames():
                                break      # closes the stream via `with`
                        chunk = resp.read(16384)
                        if not chunk:
                            raise ConnectionError("camera stream ended")
                        buf += chunk
                        while True:
                            start = buf.find(SOI)
                            if start < 0:
                                # Keep a trailing 0xFF: it may be the first half
                                # of an SOI split across two chunks.
                                buf = buf[-1:] if buf.endswith(b"\xff") else b""
                                break
                            end = buf.find(EOI, start + 2)
                            if end < 0:
                                buf = buf[start:]
                                break
                            self._latest_jpeg = buf[start:end + 2]
                            self._latest_jpeg_at = time.monotonic()
                            buf = buf[end + 2:]
                            frames += 1
                            now = time.monotonic()
                            if now - t0 >= 2.0:
                                self._bridge_fps = round(frames / (now - t0), 1)
                                frames, t0 = 0, now
            except Exception as e:
                self._bridge_fps = 0.0
                self.get_logger().warning(
                    f"camera stream disconnected ({e}); retrying in 2 s",
                    throttle_duration_sec=30.0)
                self._bridge_stop.wait(2.0)

    def _bridge_http(self, method, path, payload=None, timeout=12.0):
        """Small JSON call to the camera server."""
        data = json.dumps(payload or {}).encode() if method == "POST" else None
        req = urllib.request.Request(f"{self.bridge_url}{path}", data=data,
                                     headers={"Content-Type": "application/json"},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            # The server says WHY in the body ({"error": ...}); "HTTP Error
            # 409: Conflict" on its own sends the reader to the wrong place.
            try:
                reason = json.loads(exc.read().decode() or "{}").get("error")
            except Exception:
                reason = None
            raise RuntimeError(f"camera server {exc.code}: "
                               f"{reason or exc.reason}") from exc

    # ------------------------------------------------------------------ #
    #  Control math
    # ------------------------------------------------------------------ #
    def _apply_controls(self):
        if self.bridge_url is None:
            return
        # colour_gain is folded into the red/blue gains the server applies.
        cg = self.cam["colour_gain"]
        self._bridge_http("POST", "/controls", {
            "red_gain": self.cam["red_gain"] * cg,
            "blue_gain": self.cam["blue_gain"] * cg,
            "exposure": int(self.cam["exposure"]),
            "analogue_gain": self.cam["analogue_gain"],
            "contrast": self.cam["contrast"], "saturation": self.cam["saturation"],
            "brightness": self.cam["brightness"], "sharpness": self.cam["sharpness"],
        })

    def _apply_framerate(self, fps):
        """Set capture fps and derive the exposure/gain that achieves it without
        darkening (constant exposure_budget = exposure_us * analogue_gain)."""
        fps = max(MIN_FPS, min(MAX_FPS, float(fps)))
        self.cam["framerate"] = fps
        max_exposure_us = int((1_000_000.0 / fps) * 0.92)
        exposure_us = max(100, int(min(self.exposure_budget, max_exposure_us)))
        analogue_gain = max(1.0, min(16.0, self.exposure_budget / exposure_us))
        self.cam["exposure"] = exposure_us
        self.cam["analogue_gain"] = round(analogue_gain, 3)
        if self.bridge_url is not None:
            self._bridge_http("POST", "/controls", {
                "framerate": fps, "exposure": exposure_us,
                "analogue_gain": self.cam["analogue_gain"],
            })

    # ------------------------------------------------------------------ #
    #  Publishers
    # ------------------------------------------------------------------ #
    def _publish_frame(self):
        # Republish the newest ingested JPEG verbatim, ONCE. This timer runs at
        # publish_fps regardless of what the camera server manages, so without
        # the freshness check a slow camera looks like a fast one and
        # subscribers get the same frame several times -- which quietly breaks
        # anything downstream that measures motion between frames.
        if self._latest_jpeg is None or self._latest_jpeg_at <= self._published_at:
            return
        self._published_at = self._latest_jpeg_at
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "camera"
        msg.format = "jpeg"
        msg.data = bytes(self._latest_jpeg)
        self.image_pub.publish(msg)

    def _sync_bridge_controls(self):
        """Push this node's settings to the camera server the first time it
        answers, so `camera/state` and the camera cannot disagree about what
        the camera is doing.

        On the CONNECT EDGE, not at startup: the camera server comes up before
        the sensor does (it serves 503 while retrying), so a one-shot push in
        __init__ just fails once and leaves the two permanently out of sync.
        The flag resets when the server goes away, so a camera restart re-syncs."""
        if self.bridge_url is None:
            return
        if not self.connected:
            self._pushed_controls = False
            return
        if self._pushed_controls or time.monotonic() < self._push_retry_at:
            return
        # Gate the RETRY, not just the log line. This runs on a 2 Hz timer and
        # each attempt is a blocking HTTP call with a 12 s timeout, so an
        # ungated retry hammers the camera server and stacks timer threads.
        self._push_retry_at = time.monotonic() + 10.0
        try:
            self._apply_framerate(self.cam["framerate"])
            self._apply_controls()
            self._pushed_controls = True
            self.get_logger().info("Pushed camera settings to the camera server.")
        except Exception as exc:
            self.get_logger().warning(f"could not push camera settings ({exc}); "
                                      "retrying in 10 s", throttle_duration_sec=30.0)

    # Server /controls key -> this node's self.cam key, for the read-back.
    _SERVER_FIELDS = ("exposure", "analogue_gain", "contrast", "saturation",
                      "brightness", "sharpness")

    def _refresh_from_server(self):
        """Adopt what the camera server is ACTUALLY doing.

        The server is the camera's only owner, and there are two ways to change
        it: the ROS services here, and the gateway's /camera/controls and
        /camera/mode, which go straight to the server (the SDK and the MCP tools
        use those). Reading back only the geometry left camera/state reporting
        this node's last push while the camera ran on whatever a client set
        through the other door. So every SERVER_POLL_S this adopts:

          * geometry -- camera/mode switches between sensor modes of different
            size, and every client's micrometres-per-pixel is computed against
            the width reported here;
          * frame rate, exposure, gain, colour gains and the image controls --
            and re-derives the exposure budget from them, so the next
            set_framerate keeps the brightness the camera really has;
          * the measured fps, from the server's encoder frame counter (the one
            number that is right whether or not this node is ingesting).

        One loopback GET per poll, gated so a hung camera server cannot stack
        blocking calls on the 2 Hz state timer.
        """
        if self.bridge_url is None or time.monotonic() < self._refresh_at:
            return
        self._refresh_at = time.monotonic() + SERVER_POLL_S
        try:
            info = self._bridge_http("GET", "/controls", timeout=2.0)
        except Exception:
            self._server_fps = 0.0
            return
        now = time.monotonic()
        self._server_ok_at = now

        frames = info.get("frames")
        if isinstance(frames, int):
            prev = self._server_frames
            if prev is not None and frames >= prev[0] and now > prev[1]:
                self._server_fps = round((frames - prev[0]) / (now - prev[1]), 1)
            self._server_frames = (frames, now)

        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if width and height and (width, height) != (self.width, self.height):
            self.get_logger().info(
                f"camera geometry now {width}x{height} (mode {info.get('mode')})")
            self.width, self.height = width, height
        # Settings are adopted only once THIS connection has had our push (see
        # _sync_bridge_controls). A restarted server comes back on defaults;
        # adopting those first would wipe the settings the push is there to
        # restore.
        if not self._pushed_controls:
            return
        if info.get("framerate"):
            self.cam["framerate"] = float(info["framerate"])
        for key in self._SERVER_FIELDS:
            val = info.get(key)
            if isinstance(val, (int, float)) and math.isfinite(val):
                self.cam[key] = val
        # The server applies red/blue * colour_gain (see _apply_controls), so
        # undo the fold to keep colour_gain meaningful.
        cg = self.cam["colour_gain"] or 1.0
        for key in ("red_gain", "blue_gain"):
            val = info.get(key)
            if isinstance(val, (int, float)) and math.isfinite(val) and val > 0:
                self.cam[key] = round(val / cg, 3)
        if self.cam["exposure"] > 0 and self.cam["analogue_gain"] > 0:
            self.exposure_budget = self.cam["exposure"] * self.cam["analogue_gain"]

    def _publish_state(self):
        self._refresh_from_server()
        self._sync_bridge_controls()
        msg = CameraState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.connected = self.connected
        msg.target_fps = float(self.cam["framerate"])
        msg.measured_fps = float(self._server_fps or self._bridge_fps)
        msg.exposure_us = int(self.cam["exposure"])
        msg.analogue_gain = float(self.cam["analogue_gain"])
        msg.red_gain = float(self.cam["red_gain"])
        msg.green_gain = float(self.cam["green_gain"])
        msg.blue_gain = float(self.cam["blue_gain"])
        msg.colour_gain = float(self.cam["colour_gain"])
        msg.contrast = float(self.cam["contrast"])
        msg.saturation = float(self.cam["saturation"])
        msg.brightness = float(self.cam["brightness"])
        msg.sharpness = float(self.cam["sharpness"])
        msg.width = int(self.width)
        msg.height = int(self.height)
        self.state_pub.publish(msg)

    # ------------------------------------------------------------------ #
    #  Services
    # ------------------------------------------------------------------ #
    # NaN means "leave this one alone" (see SetCameraControls.srv). A gain or
    # scale of 0 is not a setting anybody wants -- it is a partially-filled
    # request -- so it is refused rather than obeyed. brightness is the
    # exception: 0.0 is its neutral value.
    _POSITIVE_ONLY = ("red_gain", "green_gain", "blue_gain", "colour_gain")

    def _on_set_controls(self, request, response):
        if not self.connected:
            response.success = False
            response.message = "Camera unavailable"
            return response
        try:
            # A manual analogue-gain change is treated as a brightness (budget) change.
            g = float(request.analogue_gain)
            if not math.isnan(g):
                if g <= 0:
                    raise ValueError("analogue_gain must be > 0 (NaN = leave unchanged)")
                self.exposure_budget = self.cam["exposure"] * max(1.0, min(16.0, g))
                self._apply_framerate(self.cam["framerate"])
            for key in self._POSITIVE_ONLY + ("contrast", "saturation",
                                              "brightness", "sharpness"):
                val = float(getattr(request, key))
                if math.isnan(val):
                    continue
                if val <= 0 and key in self._POSITIVE_ONLY:
                    raise ValueError(f"{key} must be > 0 (NaN = leave unchanged)")
                self.cam[key] = val
            self._apply_controls()
            response.success = True
            response.message = "ok"
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    def _on_set_framerate(self, request, response):
        try:
            if math.isnan(request.fps):
                raise ValueError("fps is required")
            self._apply_framerate(request.fps)
            self._apply_controls()
            response.success = self.connected
        except Exception as e:
            self.get_logger().warning(f"set_framerate failed: {e}")
            response.success = False
        response.framerate = float(self.cam["framerate"])
        response.exposure_us = int(self.cam["exposure"])
        response.analogue_gain = float(self.cam["analogue_gain"])
        return response

    def _on_white_balance(self, request, response):
        """One-shot hardware AWB, run by the camera server: it enables AWB, lets
        it settle, reads the gains it chose and freezes them."""
        if not self.connected:
            response.success = False
            response.message = "Camera unavailable"
            return response
        try:
            out = self._bridge_http("POST", "/white_balance", {}, timeout=15.0)
            if "error" in out:
                response.success = False
                response.message = str(out["error"])
                return response
            self.cam["red_gain"] = round(float(out["red_gain"]), 2)
            self.cam["blue_gain"] = round(float(out["blue_gain"]), 2)
            self.cam["colour_gain"] = 1.0
            response.success = True
            response.red_gain = self.cam["red_gain"]
            response.blue_gain = self.cam["blue_gain"]
            response.message = "ok"
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    # ------------------------------------------------------------------ #
    #  Action: autofocus (camera focus metric + stage Z moves)
    # ------------------------------------------------------------------ #
    def _call_jog(self, dx, dy, dz, timeout=8.0):
        """Synchronously call the stage's jog service from this thread."""
        if not self.cli_jog.wait_for_service(timeout_sec=1.0):
            raise RuntimeError("stage/jog unavailable")
        req = StageJog.Request()
        req.dx, req.dy, req.dz = int(dx), int(dy), int(dz)
        future = self.cli_jog.call_async(req)
        done = threading.Event()
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(timeout):
            raise RuntimeError("stage/jog timed out")
        res = future.result()
        if not res.success:
            raise RuntimeError(res.message or "jog failed")
        return res

    def _focus_score(self):
        """Sharpness of a FRESH frame (variance of the Laplacian); higher =
        sharper. Waits for a frame newer than 'now' from the ingest thread, so
        a frame captured before the stage finished moving is never scored."""
        asked = time.monotonic()          # same clock as _latest_jpeg_at
        while self._latest_jpeg_at <= asked:
            # 8 s, not 5: the first score of a run also waits for the idle
            # ingest thread to notice (<= 0.5 s) and open the stream.
            if time.monotonic() - asked > 8.0:
                raise RuntimeError("no fresh frame from camera server")
            time.sleep(0.01)
        arr = self._np.frombuffer(self._latest_jpeg, dtype=self._np.uint8)
        bgr = self._cv2.imdecode(arr, self._cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError("could not decode camera frame")
        gray = self._cv2.cvtColor(bgr, self._cv2.COLOR_BGR2GRAY)
        return float(self._cv2.Laplacian(gray, self._cv2.CV_64F).var())

    def _execute_autofocus(self, goal_handle):
        # The node only ingests while frames are wanted; this run wants them.
        self._af_active = True
        try:
            return self._autofocus(goal_handle)
        finally:
            self._af_active = False

    def _autofocus(self, goal_handle):
        req = goal_handle.request
        result = Autofocus.Result()
        if not self.connected:
            goal_handle.abort()
            result.success = False
            result.message = "Camera unavailable"
            return result

        n = max(3, int(req.steps))
        z_range = max(1, int(req.z_range))
        settle = max(0.0, float(req.settle_s))
        np = self._np
        try:
            z0 = self._call_jog(0, 0, 0).z                     # read current Z
            targets = [int(z) for z in np.linspace(z0 - z_range, z0 + z_range, n)]
            current = z0

            def goto(z):
                nonlocal current
                self._call_jog(0, 0, (z - AF_BACKLASH) - current)   # approach from below
                self._call_jog(0, 0, z - (z - AF_BACKLASH))
                current = z
                if settle:
                    time.sleep(settle)

            scores = []
            for i, z in enumerate(targets):
                if goal_handle.is_cancel_requested:
                    goal_handle.canceled()
                    result.success = False
                    result.message = "canceled"
                    return result
                goto(z)
                s = self._focus_score()
                scores.append(s)
                fb = Autofocus.Feedback()
                fb.index = i
                fb.z = z
                fb.score = s
                goal_handle.publish_feedback(fb)

            best_i = int(np.argmax(scores))
            best_z = targets[best_i]
            goto(best_z)                                       # park at the sharpest Z
            goal_handle.succeed()
            result.success = True
            result.best_z = int(best_z)
            result.best_score = float(scores[best_i])
            result.message = "ok"
        except Exception as e:
            goal_handle.abort()
            result.success = False
            result.message = str(e)
        return result

    def destroy_node(self):
        self._bridge_stop.set()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CameraNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
