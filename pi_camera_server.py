#!/usr/bin/env python3
"""Minimal MJPEG camera server for the Raspberry Pi (SCOPIO).

WHY THIS EXISTS
---------------
picamera2/libcamera work natively on Raspberry Pi OS but are NOT available inside
the Ubuntu-based ROS container (libcamera would have to match the Pi kernel, which
is fragile -- see ros2_ws/Dockerfile "Camera caveat"). So, as a pragmatic bridge
that works TODAY, we run this tiny native server on the Pi HOST and let the SCOPIO
UI ingest its MJPEG stream (set CAMERA_MJPEG_URL in the UI -- see WINDOWS_CLIENT.md).

Stage/galvo control still goes through ROS; only the video rides this direct
stream. The "proper" ROS camera node (camera frames published on the ROS graph)
remains the follow-up once a container-camera route is sorted.

RUN ON THE PI HOST (not in Docker):
    python3 pi_camera_server.py
Then the stream is at:  http://<pi-ip>:8081/stream.mjpg
Over the direct cable that's:  http://10.42.0.1:8081/stream.mjpg

Env: CAM_PORT (8081), CAM_W (640), CAM_H (480).
Requires: python3-picamera2 (preinstalled on Raspberry Pi OS Bookworm).
"""

import io
import os
import socketserver
from http import server
from threading import Condition

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

PORT = int(os.environ.get("CAM_PORT", 8081))
SIZE = (int(os.environ.get("CAM_W", 640)), int(os.environ.get("CAM_H", 480)))


class StreamingOutput(io.BufferedIOBase):
    """picamera2 writes each JPEG frame here; readers wait on the condition."""

    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


output = StreamingOutput()


class Handler(server.BaseHTTPRequestHandler):
    def log_message(self, *a):        # quiet
        pass

    def do_GET(self):
        if self.path not in ("/", "/stream.mjpg"):
            self.send_error(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        try:
            while True:
                with output.condition:
                    output.condition.wait()
                    frame = output.frame
                self.wfile.write(b"--FRAME\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass          # client disconnected; just end this handler


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(main={"size": SIZE}))
    picam2.start_recording(MJPEGEncoder(), FileOutput(output))
    print(f"SCOPIO Pi camera MJPEG server on http://0.0.0.0:{PORT}/stream.mjpg "
          f"({SIZE[0]}x{SIZE[1]})")
    try:
        StreamingServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        picam2.stop_recording()


if __name__ == "__main__":
    main()
