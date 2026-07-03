#!/usr/bin/env python3
"""Camera stream + control server for the Raspberry Pi (SCOPIO).

WHY THIS EXISTS
---------------
picamera2/libcamera work natively on Raspberry Pi OS but NOT inside the Ubuntu ROS
container (libcamera would have to match the Pi kernel -- fragile). So the camera
lives here, on the Pi host, and the SCOPIO UI drives it over HTTP:

  GET  /stream.mjpg   -> live MJPEG video (the UI ingests this into /video_feed)
  GET  /controls      -> current camera settings (incl. live exposure/gain)
  POST /controls      -> set framerate/exposure/gain/colour/contrast/... (JSON)
  POST /white_balance -> one-shot auto white balance; locks the measured gains
  GET  /focus         -> a focus metric (JPEG size) for autofocus

Stage/galvo control still go through ROS; only the camera is served here. Autofocus
is orchestrated by the UI (it moves Z over ROS and reads sharpness from the frames).

RUN ON THE PI HOST (not in Docker):
    python3 pi_camera_server.py            # http://<pi>:8081
Env: CAM_PORT (8081), CAM_W (640), CAM_H (480).
Requires: python3-picamera2 (preinstalled on Raspberry Pi OS Bookworm).
"""

import io
import os
import json
import time
import socketserver
from http import server
from threading import Condition

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

PORT = int(os.environ.get("CAM_PORT", 8081))
SIZE = (int(os.environ.get("CAM_W", 640)), int(os.environ.get("CAM_H", 480)))

picam2 = None

# Last-commanded settings, echoed back by GET /controls (merged with live metadata).
state = {
    "framerate": 30.0, "exposure": 20000, "analogue_gain": 1.0,
    "red_gain": 2.4, "blue_gain": 2.5, "green_gain": 1.0, "colour_gain": 1.0,
    "contrast": 1.0, "saturation": 1.0, "brightness": 0.0, "sharpness": 1.0,
}


def _num(v):
    """Return float(v) if v is a real number, else None (skips NaN / None / bad)."""
    try:
        f = float(v)
        return f if f == f else None      # f != f is True only for NaN
    except (TypeError, ValueError):
        return None


def apply_controls(d):
    """Apply a partial dict of settings to the camera. Keys match the UI:
    framerate, exposure, analogue_gain, red_gain, blue_gain, contrast,
    saturation, brightness, sharpness."""
    c = {}
    fps = _num(d.get("framerate"))
    if fps:
        fps = max(1.0, min(120.0, fps))
        dur = int(1_000_000 / fps)
        c["FrameDurationLimits"] = (dur, dur)
        state["framerate"] = fps
    exp = _num(d.get("exposure"))
    if exp:
        c["AeEnable"] = False
        c["ExposureTime"] = int(exp)
        state["exposure"] = int(exp)
    ag = _num(d.get("analogue_gain"))
    if ag:
        c["AeEnable"] = False
        c["AnalogueGain"] = ag
        state["analogue_gain"] = ag
    red, blue = _num(d.get("red_gain")), _num(d.get("blue_gain"))
    if red is not None or blue is not None:
        r = red if red is not None else state["red_gain"]
        b = blue if blue is not None else state["blue_gain"]
        c["AwbEnable"] = False
        c["ColourGains"] = (r, b)
        state["red_gain"], state["blue_gain"] = r, b
    for key, ctrl in (("contrast", "Contrast"), ("saturation", "Saturation"),
                      ("brightness", "Brightness"), ("sharpness", "Sharpness")):
        val = _num(d.get(key))
        if val is not None:
            c[ctrl] = val
            state[key] = val
    if c and picam2 is not None:
        picam2.set_controls(c)
    return get_controls()


def do_white_balance():
    """One-shot AWB: enable auto, let it settle, read the measured colour gains,
    then lock them in as manual gains (so they don't drift). Returns the gains."""
    if picam2 is None:
        return {"error": "no camera"}
    picam2.set_controls({"AwbEnable": True})
    time.sleep(1.2)
    md = picam2.capture_metadata()
    gains = md.get("ColourGains")
    if not gains:
        return {"error": "no gains reported"}
    r, b = float(gains[0]), float(gains[1])
    picam2.set_controls({"AwbEnable": False, "ColourGains": (r, b)})
    state["red_gain"], state["blue_gain"] = r, b
    return {"red_gain": r, "blue_gain": b}


def get_controls():
    """Current settings, merging commanded state with live camera metadata."""
    out = dict(state)
    try:
        md = picam2.capture_metadata() if picam2 is not None else {}
        if "ExposureTime" in md:
            out["exposure"] = int(md["ExposureTime"])
        if "AnalogueGain" in md:
            out["analogue_gain"] = round(float(md["AnalogueGain"]), 3)
        if md.get("ColourGains"):
            out["red_gain"] = round(float(md["ColourGains"][0]), 3)
            out["blue_gain"] = round(float(md["ColourGains"][1]), 3)
    except Exception:
        pass
    return out


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


output = StreamingOutput()


class Handler(server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/stream.mjpg"):
            self._stream()
        elif self.path == "/controls":
            self._json(get_controls())
        elif self.path == "/focus":
            self._json({"metric": len(output.frame or b"")})
        else:
            self.send_error(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            d = {}
        if self.path == "/controls":
            self._json(apply_controls(d))
        elif self.path == "/white_balance":
            self._json(do_white_balance())
        else:
            self.send_error(404)
            self.end_headers()

    def _stream(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
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
            pass


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global picam2
    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(main={"size": SIZE}))
    picam2.start_recording(MJPEGEncoder(), FileOutput(output))
    print(f"SCOPIO Pi camera server on http://0.0.0.0:{PORT} "
          f"(stream /stream.mjpg, controls /controls) {SIZE[0]}x{SIZE[1]}")
    try:
        StreamingServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        picam2.stop_recording()


if __name__ == "__main__":
    main()
