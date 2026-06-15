"""Computer-vision: circle detection and the live tracking worker.

``tracking_worker`` is the bridge loop between the camera and the UI. It pulls
the lightweight ``lores`` stream from the camera, runs Hough-circle detection
on it, draws the overlay, publishes the JPEG display frame back to
``camera.current_jpeg`` and pushes circle coordinates to SSE clients.

State that the rest of the app reassigns (``tracking_enabled``,
``tracking_interval_ms``/``_sec``) must be set as ``ML.<name> = ...`` so this
module sees the change.
"""

import time
import json
import gc
import threading

import cv2
import numpy as np
from simplejpeg import encode_jpeg

import software.camera as camera

# ========== Tracking settings/state ==========
tracking_enabled = True
tracking_interval_ms = 100
tracking_interval_sec = 0.1

current_circle = None
last_tracking_time = 0


class ReliableSSEClient:
    def __init__(self):
        self.latest = None
        self.available = False
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)

    def put(self, data):
        with self.cond:
            self.latest = data
            self.available = True
            self.cond.notify()

    def get(self):
        with self.cond:
            while not self.available:
                self.cond.wait()
            data = self.latest
            self.available = False
            return data


tracking_clients = []
tracking_lock = threading.Lock()


def detect_circle(image_bgr_small):
    gray = cv2.cvtColor(image_bgr_small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 1.5)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT,
        dp=1.2, minDist=40,
        param1=50, param2=35,
        minRadius=10, maxRadius=80,
    )
    if circles is not None:
        circles = np.round(circles[0, :]).astype(int)
        largest = max(circles, key=lambda c: c[2])
        return tuple(largest)
    return None


def tracking_worker():
    global current_circle, last_tracking_time

    # FIX #7: frame_counter를 0부터 시작 → 첫 프레임부터 탐지
    frame_counter = 0
    frame_interval = 1.0 / camera.STREAM_FPS
    next_frame_time = time.perf_counter()

    last_sse_time = 0.0
    last_jpeg_time = 0.0
    last_detected_circle = None

    while True:
        now = time.perf_counter()
        if now < next_frame_time:
            time.sleep(next_frame_time - now)
        next_frame_time = time.perf_counter() + frame_interval

        if not camera.camera_running or camera.picam2 is None:
            continue

        if camera.calibration_running:
            time.sleep(0.05)
            continue

        try:
            with camera.camera_use_lock:
                if camera.calibration_running:
                    continue
                with camera.camera_lock:
                    frame_yuv = camera.picam2.capture_array("lores")
            if frame_yuv is None:
                continue

            frame_small = cv2.cvtColor(frame_yuv, cv2.COLOR_YUV2BGR_I420)

            # FIX #6: 불필요한 .copy() 제거 + inplace 연산으로 메모리 절약
            gg = camera.cam_controls.get("green_gain", 1.0)
            if abs(gg - 1.0) > 0.005:
                green = frame_small[:, :, 1].astype(np.float32)
                np.multiply(green, gg, out=green)
                np.clip(green, 0, 255, out=green)
                frame_small[:, :, 1] = green.astype(np.uint8)

            # FIX #7: frame_counter를 먼저 증가시킨 뒤 % 체크 → 0번째(첫 프레임) 탐지
            frame_counter += 1
            circle_small = None
            if tracking_enabled and (frame_counter % camera.FRAME_SKIP == 0):
                circle_small = detect_circle(frame_small)

            if circle_small is not None:
                last_detected_circle = circle_small

            if last_detected_circle is not None:
                x, y, r = last_detected_circle
                circle_display = (x * 2, y * 2, r * 2)
            else:
                circle_display = None

            current_circle = circle_display

            if camera.is_recording and circle_small is not None:
                now_sec = time.time()
                if now_sec - last_tracking_time >= tracking_interval_sec:
                    last_tracking_time = now_sec
                    timestamp_ms = int(now_sec * 1000)
                    x, y, r = circle_small
                    camera.recording_coordinates.append([timestamp_ms, int(x), int(y), int(r)])

            frame_display = cv2.resize(frame_small, (640, 480))
            if circle_display is not None:
                xd, yd, rd = circle_display
                cv2.circle(frame_display, (xd, yd), rd, (0, 0, 255), 2)
                cv2.circle(frame_display, (xd, yd), 3, (0, 0, 255), -1)
                cv2.putText(
                    frame_display, f"({xd},{yd}) r={rd}", (xd + 10, yd - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1,
                )
            else:
                cv2.putText(
                    frame_display, "No circle", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2,
                )

            now_time = time.time()
            if now_time - last_jpeg_time >= (1 / 15):
                last_jpeg_time = now_time
                new_jpeg = encode_jpeg(frame_display, quality=60, colorspace="BGR")
                # FIX #5: Lock으로 current_jpeg 보호
                with camera._jpeg_lock:
                    camera.current_jpeg = new_jpeg

            if now_time - last_sse_time >= 0.1:
                last_sse_time = now_time
                if last_detected_circle is not None:
                    x, y, r = last_detected_circle
                    circle_json = [int(x * 2), int(y * 2), int(r * 2)]
                else:
                    circle_json = None
                data = json.dumps({"circle": circle_json})
                # FIX #10: 순회 중 리스트 변경 방지 → 스냅샷으로 순회
                with tracking_lock:
                    clients_snapshot = list(tracking_clients)
                for client in clients_snapshot:
                    try:
                        client.put(data)
                    except Exception:
                        pass

            if frame_counter % 100 == 0:
                print(f"\n==== PERF ==== clients: {len(tracking_clients)} ============\n")

            if frame_counter % 300 == 0:
                gc.collect()

        except Exception as e:
            print(f"Tracking worker error: {e}")
            time.sleep(0.1)


def event_stream():
    client = ReliableSSEClient()
    with tracking_lock:
        tracking_clients.append(client)
    try:
        while True:
            data = client.get()
            yield f"data: {data}\n\n"
    finally:
        with tracking_lock:
            if client in tracking_clients:
                tracking_clients.remove(client)
