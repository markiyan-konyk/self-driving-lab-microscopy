"""camera_node - the sole owner of the Pi camera (picamera2).

Publishes the live view as a CompressedImage and records H.264 on request. It
is the only node that touches picamera2, so there is a single owner of the
sensor (the tracker subscribes to the published image rather than grabbing
frames itself).

Topics / services (under /scopio):
  pub  image/compressed   sensor_msgs/CompressedImage
  pub  recording/status   scopio_interfaces/RecordingStatus
  srv  recording/set      scopio_interfaces/SetRecording

Degrades gracefully: with no camera present it logs a warning and idles, so the
rest of the graph still comes up.
"""

import os
import threading
import time

import rclpy
from rclpy.node import Node

from sensor_msgs.msg import CompressedImage
from scopio_interfaces.msg import RecordingStatus
from scopio_interfaces.srv import SetRecording


def _now_stamp(node):
    return node.get_clock().now().to_msg()


class CameraNode(Node):
    def __init__(self):
        super().__init__("camera_node")
        self.declare_parameter("width", 640)
        self.declare_parameter("height", 480)
        self.declare_parameter("framerate", 30.0)
        self.declare_parameter("publish_fps", 15.0)
        self.declare_parameter("jpeg_quality", 70)
        self.declare_parameter("recordings_dir", "recordings")

        self.width = self.get_parameter("width").value
        self.height = self.get_parameter("height").value
        self.framerate = self.get_parameter("framerate").value
        self.jpeg_quality = int(self.get_parameter("jpeg_quality").value)
        self.recordings_dir = self.get_parameter("recordings_dir").value

        self._lock = threading.Lock()          # serialize picam2 access
        self.picam2 = None
        self._encode = None                     # jpeg encoder fn
        self._yuv2bgr = None

        # Recording state
        self.is_recording = False
        self.rec_filename = ""
        self.rec_duration = 0.0
        self.rec_started_at = 0.0
        self._encoder = None

        self.image_pub = self.create_publisher(CompressedImage, "image/compressed", 5)
        self.rec_pub = self.create_publisher(RecordingStatus, "recording/status", 5)
        self.create_service(SetRecording, "recording/set", self._on_set_recording)

        self._start_camera()

        publish_fps = max(1.0, self.get_parameter("publish_fps").value)
        self.create_timer(1.0 / publish_fps, self._publish_frame)
        self.create_timer(1.0, self._publish_status)

    # ------------------------------------------------------------------ #
    def _start_camera(self):
        try:
            from picamera2 import Picamera2
            import cv2
            self._yuv2bgr = lambda f: cv2.cvtColor(f, cv2.COLOR_YUV2BGR_I420)
            try:
                from simplejpeg import encode_jpeg
                self._encode = lambda bgr: encode_jpeg(
                    bgr, quality=self.jpeg_quality, colorspace="BGR")
            except Exception:
                self._encode = lambda bgr: cv2.imencode(
                    ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])[1].tobytes()

            picam2 = Picamera2()
            config = picam2.create_video_configuration(
                main={"size": (self.width, self.height), "format": "YUV420"})
            picam2.configure(config)
            picam2.set_controls({"FrameRate": self.framerate})
            picam2.start()
            self.picam2 = picam2
            self.get_logger().info(
                f"Camera started {self.width}x{self.height} @ {self.framerate} fps")
        except Exception as e:
            self.get_logger().warning(f"Camera unavailable ({e}); idling.")
            self.picam2 = None

    def _publish_frame(self):
        if self.picam2 is None:
            return
        try:
            with self._lock:
                frame_yuv = self.picam2.capture_array("main")
            bgr = self._yuv2bgr(frame_yuv)
            jpeg = self._encode(bgr)
            msg = CompressedImage()
            msg.header.stamp = _now_stamp(self)
            msg.header.frame_id = "camera"
            msg.format = "jpeg"
            msg.data = bytes(jpeg)
            self.image_pub.publish(msg)
        except Exception as e:
            self.get_logger().warning(f"capture/publish failed: {e}")

    # ------------------------------------------------------------------ #
    #  Recording
    # ------------------------------------------------------------------ #
    def _on_set_recording(self, request, response):
        if self.picam2 is None:
            response.success = False
            response.message = "Camera unavailable"
            return response
        try:
            if request.start:
                response.filename = self._start_recording(request.duration_s)
                response.success = True
                response.message = "recording"
            else:
                self._stop_recording()
                response.success = True
                response.message = "stopped"
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    def _start_recording(self, duration_s):
        if self.is_recording:
            return self.rec_filename
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FfmpegOutput
        os.makedirs(self.recordings_dir, exist_ok=True)
        idx = len([n for n in os.listdir(self.recordings_dir)
                   if n.lower().endswith(".mp4")]) + 1
        dur_tag = f"{int(duration_s)}s" if duration_s and duration_s > 0 else "inf"
        name = f"recording_{idx}_{int(self.framerate)}fps_{dur_tag}.mp4"
        path = os.path.join(self.recordings_dir, name)
        self._encoder = H264Encoder()
        with self._lock:
            self.picam2.start_encoder(self._encoder, FfmpegOutput(path))
        self.is_recording = True
        self.rec_filename = name
        self.rec_duration = float(duration_s)
        self.rec_started_at = time.time()
        self.get_logger().info(f"Recording -> {name}")
        return name

    def _stop_recording(self):
        if not self.is_recording:
            return
        try:
            with self._lock:
                self.picam2.stop_encoder()
        finally:
            self.is_recording = False
            self._encoder = None
            self.get_logger().info(f"Recording stopped: {self.rec_filename}")

    def _publish_status(self):
        # Auto-stop when a finite duration elapses.
        if self.is_recording and self.rec_duration > 0:
            if time.time() - self.rec_started_at >= self.rec_duration:
                self._stop_recording()
        msg = RecordingStatus()
        msg.header.stamp = _now_stamp(self)
        msg.recording = self.is_recording
        msg.duration_s = float(self.rec_duration)
        if self.is_recording and self.rec_duration > 0:
            msg.remaining_s = max(
                0.0, self.rec_duration - (time.time() - self.rec_started_at))
        else:
            msg.remaining_s = 0.0
        msg.filename = self.rec_filename if self.is_recording else ""
        self.rec_pub.publish(msg)

    def destroy_node(self):
        try:
            self._stop_recording()
            if self.picam2 is not None:
                self.picam2.stop()
        except Exception:
            pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CameraNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
