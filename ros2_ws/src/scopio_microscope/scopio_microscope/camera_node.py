"""camera_node - the sole owner of the Pi camera (picamera2).

A PURE SENSOR + control surface. It publishes the live view as a
CompressedImage and exposes the camera's manual controls, the single
frame-rate knob (which auto-derives exposure/gain), and a one-shot hardware
white balance. It is the only node that touches picamera2.

It deliberately does NOT record. Recording is a *client* concern: the UI (or
any program) subscribes to image/compressed and saves to its own local folder,
so the Pi takes no extra recording/disk load and footage lives with whoever is
watching.

Topics / services (under /scopio):
  pub  image/compressed   sensor_msgs/CompressedImage      (JPEG live view)
  pub  camera/state       scopio_interfaces/CameraState    (settings + real fps)
  srv  camera/set_controls  scopio_interfaces/SetCameraControls
  srv  camera/set_framerate scopio_interfaces/SetFramerate
  srv  camera/white_balance scopio_interfaces/WhiteBalance

The frame-rate -> exposure/gain "budget" math and the hardware-AWB routine are
ported from the validated monolith (microscope/camera.py, white_balance.py).

Degrades gracefully: with no camera present it logs a warning and idles, so the
rest of the graph still comes up.
"""

import math
import threading
import time

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from sensor_msgs.msg import CompressedImage
from scopio_interfaces.msg import CameraState
from scopio_interfaces.srv import SetCameraControls, SetFramerate, WhiteBalance

MIN_FPS, MAX_FPS = 1.0, 120.0


class CameraNode(Node):
    def __init__(self):
        super().__init__("camera_node")
        self.declare_parameter("width", 640)
        self.declare_parameter("height", 480)
        self.declare_parameter("framerate", 30.0)
        self.declare_parameter("publish_fps", 15.0)
        self.declare_parameter("jpeg_quality", 70)

        self.width = int(self.get_parameter("width").value)
        self.height = int(self.get_parameter("height").value)
        self.jpeg_quality = int(self.get_parameter("jpeg_quality").value)

        # Live control state (mirrors microscope/camera.py cam_controls).
        self.cam = {
            "red_gain": 2.4, "green_gain": 1.0, "blue_gain": 2.5,
            "framerate": float(self.get_parameter("framerate").value),
            "exposure": 20000, "analogue_gain": 1.0, "colour_gain": 1.0,
            "contrast": 1.0, "saturation": 1.0, "brightness": 0.0, "sharpness": 1.0,
        }
        # Brightness target: exposure_us * analogue_gain held constant as fps changes.
        self.exposure_budget = self.cam["exposure"] * self.cam["analogue_gain"]
        self.measured_fps = 0.0

        self._lock = threading.Lock()          # serialize picam2 access
        self.picam2 = None
        self._encode = None
        self._yuv2bgr = None
        self._np = None
        self._last_meta = 0.0

        self.image_pub = self.create_publisher(CompressedImage, "image/compressed", 5)
        self.state_pub = self.create_publisher(CameraState, "camera/state", 5)

        cb = ReentrantCallbackGroup()
        self.create_service(SetCameraControls, "camera/set_controls", self._on_set_controls, callback_group=cb)
        self.create_service(SetFramerate, "camera/set_framerate", self._on_set_framerate, callback_group=cb)
        self.create_service(WhiteBalance, "camera/white_balance", self._on_white_balance, callback_group=cb)

        self._start_camera()

        publish_fps = max(1.0, float(self.get_parameter("publish_fps").value))
        self.create_timer(1.0 / publish_fps, self._publish_frame, callback_group=cb)
        self.create_timer(0.5, self._publish_state, callback_group=cb)

    # ------------------------------------------------------------------ #
    #  Camera lifecycle
    # ------------------------------------------------------------------ #
    def _start_camera(self):
        try:
            from picamera2 import Picamera2
            import cv2
            import numpy as np
            self._np = np
            self._yuv2bgr = lambda f: cv2.cvtColor(f, cv2.COLOR_YUV2BGR_I420)
            try:
                from simplejpeg import encode_jpeg
                self._encode = lambda bgr: encode_jpeg(bgr, quality=self.jpeg_quality, colorspace="BGR")
            except Exception:
                self._encode = lambda bgr: cv2.imencode(
                    ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])[1].tobytes()

            picam2 = Picamera2()
            config = picam2.create_video_configuration(
                main={"size": (self.width, self.height), "format": "YUV420"})
            picam2.configure(config)
            picam2.start()
            self.picam2 = picam2
            time.sleep(0.5)
            self._apply_framerate(self.cam["framerate"])
            self._apply_controls()
            self.get_logger().info(
                f"Camera started {self.width}x{self.height} @ {self.cam['framerate']} fps")
        except Exception as e:
            self.get_logger().warning(f"Camera unavailable ({e}); idling.")
            self.picam2 = None

    # ------------------------------------------------------------------ #
    #  Control math (ported from microscope/camera.py)
    # ------------------------------------------------------------------ #
    def _apply_controls(self):
        if self.picam2 is None:
            return
        cg = self.cam["colour_gain"]
        with self._lock:
            self.picam2.set_controls({
                "AwbEnable": False, "AeEnable": False,
                "ColourGains": (self.cam["red_gain"] * cg, self.cam["blue_gain"] * cg),
                "ExposureTime": int(self.cam["exposure"]),
                "AnalogueGain": self.cam["analogue_gain"],
                "Contrast": self.cam["contrast"], "Saturation": self.cam["saturation"],
                "Brightness": self.cam["brightness"], "Sharpness": self.cam["sharpness"],
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
        if self.picam2 is not None:
            with self._lock:
                self.picam2.set_controls({
                    "FrameRate": fps, "ExposureTime": exposure_us,
                    "AnalogueGain": self.cam["analogue_gain"],
                })

    # ------------------------------------------------------------------ #
    #  Publishers
    # ------------------------------------------------------------------ #
    def _publish_frame(self):
        if self.picam2 is None:
            return
        try:
            with self._lock:
                frame_yuv = self.picam2.capture_array("main")
                if time.time() - self._last_meta >= 0.5:
                    self._last_meta = time.time()
                    try:
                        dur = self.picam2.capture_metadata().get("FrameDuration")
                        if dur:
                            self.measured_fps = round(1_000_000.0 / dur, 1)
                    except Exception:
                        pass
            bgr = self._yuv2bgr(frame_yuv)
            gg = self.cam["green_gain"]
            if abs(gg - 1.0) > 0.005 and self._np is not None:
                green = bgr[:, :, 1].astype(self._np.float32)
                green *= gg
                bgr[:, :, 1] = green.clip(0, 255).astype(self._np.uint8)
            jpeg = self._encode(bgr)
            msg = CompressedImage()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = "camera"
            msg.format = "jpeg"
            msg.data = bytes(jpeg)
            self.image_pub.publish(msg)
        except Exception as e:
            self.get_logger().warning(f"capture/publish failed: {e}")

    def _publish_state(self):
        msg = CameraState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.connected = self.picam2 is not None
        msg.target_fps = float(self.cam["framerate"])
        msg.measured_fps = float(self.measured_fps)
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
    def _on_set_controls(self, request, response):
        if self.picam2 is None:
            response.success = False
            response.message = "Camera unavailable"
            return response
        # A manual analogue-gain change is treated as a brightness (budget) change.
        if not math.isnan(request.analogue_gain):
            g = max(1.0, min(16.0, float(request.analogue_gain)))
            self.exposure_budget = self.cam["exposure"] * g
            self._apply_framerate(self.cam["framerate"])
        for key in ("red_gain", "green_gain", "blue_gain", "colour_gain",
                    "contrast", "saturation", "brightness", "sharpness"):
            val = getattr(request, key)
            if not math.isnan(val):
                self.cam[key] = float(val)
        self._apply_controls()
        response.success = True
        response.message = "ok"
        return response

    def _on_set_framerate(self, request, response):
        self._apply_framerate(request.fps)
        self._apply_controls()
        response.success = self.picam2 is not None
        response.framerate = float(self.cam["framerate"])
        response.exposure_us = int(self.cam["exposure"])
        response.analogue_gain = float(self.cam["analogue_gain"])
        return response

    def _on_white_balance(self, request, response):
        """One-shot hardware AWB: enable AWB, let it settle, read the gains it
        chose, freeze them. Ported from microscope/white_balance.py."""
        if self.picam2 is None:
            response.success = False
            response.message = "Camera unavailable"
            return response
        try:
            with self._lock:
                self.picam2.set_controls({"AwbEnable": True, "AwbMode": 0})
                gains = None
                deadline = time.time() + 8.0
                for _ in range(40):
                    if time.time() > deadline:
                        break
                    g = self.picam2.capture_metadata().get("ColourGains")
                    if g is not None:
                        gains = g
                self.picam2.set_controls({"AwbEnable": False})
            if gains is None:
                response.success = False
                response.message = "camera did not report colour gains"
                return response
            self.cam["red_gain"] = round(float(gains[0]), 2)
            self.cam["blue_gain"] = round(float(gains[1]), 2)
            self.cam["colour_gain"] = 1.0
            self._apply_controls()
            response.success = True
            response.red_gain = self.cam["red_gain"]
            response.blue_gain = self.cam["blue_gain"]
            response.message = "ok"
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    def destroy_node(self):
        try:
            if self.picam2 is not None:
                self.picam2.stop()
        except Exception:
            pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CameraNode()
    executor = MultiThreadedExecutor()
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
