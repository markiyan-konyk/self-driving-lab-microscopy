"""Camera setup, live camera controls, calibration, and video recording.

Owns all camera-related state and the recording pipeline. Two streams are
configured on the Picamera2:

  * ``main``  640x480 YUV420  -> full-resolution stream used for H264 recording
  * ``lores`` 320x240 YUV420  -> lightweight stream consumed by the ML worker
                                 (``ML.tracking_worker``) for circle detection

The frame-rate constants (STREAM_FPS / DETECTION_FPS / FRAME_SKIP) live here
because they configure the camera; ``ML`` imports them from this module.

Calibration (autofocus / white balance) also lives here: the worker threads
take exclusive camera ownership via ``camera_use_lock`` and the
``calibration_running`` flag that ``ML.tracking_worker`` checks.
"""

import os
import re
import time
import csv
import threading

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

from autofocus import fast_autofocus, looping_autofocus
from white_balance import run_white_balance

# ========== Frame rate / detection cadence ==========
STREAM_FPS = 30
DETECTION_FPS = 10
FRAME_SKIP = STREAM_FPS // DETECTION_FPS

# ========== Recording settings ==========
record_duration = 10
record_framerate = 100

# ========== Camera state ==========
picam2 = None
camera_running = False

# FIX #2 (데드락): camera_lock을 RLock으로 교체
# → start_camera()가 camera_lock을 잡은 상태에서 자기 자신을 다시 호출해도 안전
camera_lock = threading.RLock()

# FIX #3 (레이스 컨디션): calibration_running bool → threading.Lock()으로 교체
# → check-and-set을 원자적으로 처리
_calibration_lock = threading.Lock()
calibration_running = False  # 읽기 전용 플래그 (UI/worker에서 읽기만 함)

# camera_use_lock: tracking_worker ↔ calibration 스레드 간 카메라 독점 제어
camera_use_lock = threading.Lock()

# Latest JPEG-encoded display frame (produced by ML.tracking_worker,
# consumed by the /video_feed route).
# FIX #5: current_jpeg를 Lock으로 보호 (video_feed와 tracking_worker 간 torn-read 방지)
current_jpeg = None
_jpeg_lock = threading.Lock()

# ========== Camera controls ==========
# NOTE: green_gain은 picamera2 API에 직접 없으므로 소프트웨어 처리(tracking_worker)에서만 적용됨.
#       나머지 항목들은 apply_camera_controls()에서 하드웨어에 직접 적용됨.
cam_controls = {
    "red_gain": 1.5,
    "green_gain": 1.0,   # 소프트웨어 처리 전용 (picamera2 ColourGains에는 R, B만 있음)
    "blue_gain": 2.7,
    "exposure": 10000,
    "analogue_gain": 1.0,
    "colour_gain": 1.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "brightness": 0.0,
    "sharpness": 1.0,
}

# ========== Recording state ==========
is_recording = False
recording_coordinates = []
current_recording_filename = None

# FIX #9: 녹화 취소를 위한 Event 추가
_stop_recording_event = threading.Event()


def apply_camera_controls():
    """picamera2에 하드웨어 카메라 파라미터를 적용. camera_lock 없이도 호출 가능."""
    if picam2 is None:
        return
    cg = cam_controls["colour_gain"]
    picam2.set_controls({
        "AwbEnable": False,
        "AeEnable": False,
        "ColourGains": (cam_controls["red_gain"] * cg, cam_controls["blue_gain"] * cg),
        "ExposureTime": int(cam_controls["exposure"]),
        "AnalogueGain": cam_controls["analogue_gain"],
        "Contrast": cam_controls["contrast"],
        "Saturation": cam_controls["saturation"],
        "Brightness": cam_controls["brightness"],
        "Sharpness": cam_controls["sharpness"],
    })


# ========== Camera startup ==========
def _start_camera_unlocked():
    """
    카메라를 (재)시작하는 내부 함수.
    반드시 camera_lock을 잡은 상태에서 호출해야 함.
    camera_lock이 RLock이므로 같은 스레드에서 중첩 호출해도 안전.
    """
    global picam2, camera_running
    if picam2 is not None:
        try:
            picam2.stop()
        except Exception:
            pass
        try:
            picam2.close()
        except Exception:
            pass
        picam2 = None
        camera_running = False

    picam2 = Picamera2()
    full_fov_mode = picam2.sensor_modes[0]
    config = picam2.create_video_configuration(
        sensor={"output_size": full_fov_mode["size"], "bit_depth": full_fov_mode["bit_depth"]},
        main={"size": (640, 480), "format": "YUV420"},
        lores={"size": (320, 240), "format": "YUV420"},
        controls={"FrameRate": STREAM_FPS},
    )
    picam2.configure(config)
    picam2.start()
    camera_running = True
    time.sleep(0.5)
    apply_camera_controls()
    print(f"Camera started: {STREAM_FPS} fps, detection {DETECTION_FPS} fps (skip {FRAME_SKIP})")


def start_camera():
    """공개 인터페이스: camera_lock을 획득 후 카메라 시작."""
    with camera_lock:
        _start_camera_unlocked()


# ========== Calibration helpers ==========
def _try_acquire_calibration() -> bool:
    """
    FIX #3: calibration_running의 check-and-set을 원자적으로 처리.
    성공하면 True(획득), 이미 실행 중이면 False 반환.
    """
    global calibration_running
    with _calibration_lock:
        if calibration_running:
            return False
        calibration_running = True
        return True


def _release_calibration():
    global calibration_running
    with _calibration_lock:
        calibration_running = False


def run_autofocus_thread(stage_wrapper):
    try:
        with camera_use_lock:
            best_z = looping_autofocus(
                stage_wrapper, picam2,
                dz=2000,
                n_steps=20,
                metric="jpeg_size",
                max_attempts=3,
                settle_time=0.05,
            )
            print(f"Autofocus completed. Best Z = {best_z}")
    except Exception as e:
        print(f"Autofocus error: {e}")
    finally:
        _release_calibration()


def run_white_balance_thread():
    """
    FIX #1 (데드락) + FIX #2 (카메라 중단 후 접근) 수정 버전.

    변경 전 문제:
      - run_white_balance_thread()가 camera_lock을 잡은 채 start_camera()를 호출했는데,
        start_camera() 내부에서도 camera_lock을 획득하려 해서 데드락 발생.
      - camera_lock 밖에서 멈춘 picam2에 접근.

    변경 후:
      - camera_lock을 RLock으로 교체해 재진입 허용.
      - 카메라 중단/재시작을 모두 camera_lock 안에서 처리.
      - run_white_balance()는 camera_lock을 잡은 상태에서 호출 (다른 스레드 차단).
    """
    global camera_running
    try:
        with camera_use_lock:
            # camera_lock을 잡은 채로 전체 white balance 흐름 처리
            # (RLock이므로 내부에서 start_camera() → _start_camera_unlocked() 호출 가능)
            with camera_lock:
                # 1. 스트리밍 중단
                if picam2 is not None:
                    try:
                        picam2.stop()
                    except Exception:
                        pass
                    camera_running = False

                # 2. White balance 보정 (picam2 객체에 직접 접근; 멈춘 상태에서 raw 캡처)
                red_gain, blue_gain = run_white_balance(picam2, target_white_level=876)

                # 3. 보정된 gain을 전역 cam_controls에 반영
                cam_controls["red_gain"] = red_gain
                cam_controls["blue_gain"] = blue_gain

                # 4. 카메라 재시작 (RLock이므로 같은 스레드가 재진입 가능)
                _start_camera_unlocked()

    except Exception as e:
        print(f"White balance error: {e}")
        # 카메라가 중단된 채로 에러가 났을 경우 복구 시도
        if not camera_running:
            try:
                start_camera()
            except Exception as recover_exc:
                print(f"Camera recovery failed: {recover_exc}")
    finally:
        _release_calibration()


# ========== Recording ==========
def get_next_recording_index():
    rec_dir = "recordings"
    os.makedirs(rec_dir, exist_ok=True)
    existing = [
        f for f in os.listdir(rec_dir)
        if f.startswith("recording_") and f.endswith(".mp4")
    ]
    numbers = []
    for f in existing:
        # FIX #8: 취약한 split() 파싱 → 정규식으로 교체
        m = re.match(r"recording_(\d+)_", f)
        if m:
            numbers.append(int(m.group(1)))
    return (max(numbers) + 1) if numbers else 1


def start_recording_async(duration_sec, framerate_fps, output_path):
    """
    FIX #9: time.sleep() 블로킹 대신 threading.Event.wait()로 교체
    → 녹화 도중 취소(stop_recording 엔드포인트)가 가능해짐.
    """
    global is_recording, recording_coordinates, current_recording_filename

    recording_coordinates = []
    current_recording_filename = os.path.splitext(os.path.basename(output_path))[0]
    is_recording = True
    _stop_recording_event.clear()

    max_exposure = int(1_000_000 / framerate_fps)
    rec_exposure = min(int(cam_controls["exposure"]), max_exposure)

    with camera_lock:
        if picam2 is None:
            _start_camera_unlocked()
        picam2.set_controls({"FrameRate": framerate_fps, "ExposureTime": rec_exposure})
        encoder = H264Encoder(bitrate=10_000_000)
        output = FfmpegOutput(output_path)
        picam2.start_encoder(encoder, output)

    # 취소 가능한 대기
    _stop_recording_event.wait(timeout=duration_sec)

    with camera_lock:
        picam2.stop_encoder()
        picam2.set_controls({"FrameRate": STREAM_FPS})
        apply_camera_controls()

    is_recording = False

    if recording_coordinates:
        csv_dir = "coordinates"
        os.makedirs(csv_dir, exist_ok=True)
        csv_path = os.path.join(csv_dir, f"{current_recording_filename}.csv")
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_ms", "x", "y", "radius"])
            writer.writerows(recording_coordinates)
        print(f"CSV saved: {csv_path}")
