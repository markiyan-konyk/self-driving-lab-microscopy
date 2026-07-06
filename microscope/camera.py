"""Camera setup, live camera controls, calibration, and video recording.

Owns all camera-related state and the recording pipeline. A single colour
stream is configured on the Picamera2:

  * ``main``  640x480 YUV420  -> used both for H264 recording and, after a
                                 cheap YUV->BGR conversion, for the JPEG live
                                 view produced by ``display_worker``.

Frame rate is driven by a single user-facing target (``framerate``). The
exposure needed to satisfy that frame rate is computed automatically
(``apply_framerate``): the longer a frame lasts the more light it can gather,
so higher frame rates force shorter exposures, and the analogue gain is scaled
to keep the overall brightness ("exposure budget") roughly constant.

Calibration (autofocus / white balance) takes exclusive camera ownership via
``camera_use_lock`` and the ``calibration_running`` flag that the
``display_worker`` checks.
"""

import os
import re
import time
import threading

import cv2
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput
from simplejpeg import encode_jpeg

from autofocus import looping_autofocus
from white_balance import run_white_balance

# ========== Frame rate ==========
# Default live/record frame rate. The slider in the UI drives this value via
# apply_framerate(); STREAM_FPS is just the initial value.
STREAM_FPS = 30
MIN_FPS = 1
MAX_FPS = 120

# Live JPEG display cadence (independent of capture/record rate).
DISPLAY_FPS = 15

# ========== Recording settings ==========
# Duration in seconds. ``None`` (or 0) means "record until manually stopped".
record_duration = 600  # 10 minutes default

# ========== Camera state ==========
picam2 = None
camera_running = False

# RLock so a thread already holding the lock (e.g. white balance) can call
# start_camera() -> _start_camera_unlocked() without deadlocking.
camera_lock = threading.RLock()

# Atomic check-and-set for the calibration flag.
_calibration_lock = threading.Lock()
calibration_running = False  # read-only flag for the UI / display worker

# Exclusive camera ownership between display_worker and calibration threads.
camera_use_lock = threading.Lock()

# Latest JPEG-encoded display frame (produced by display_worker,
# consumed by the /video_feed route).
current_jpeg = None
_jpeg_lock = threading.Lock()

# Real, measured frame rate (frames/sec) reported by the sensor metadata. This
# is the *actual* rate the pipeline achieves, which can be well below the
# requested rate (e.g. asking for 120 fps may only yield ~89 fps).
measured_fps = 0.0

# ========== Camera controls ==========
# ``framerate`` drives exposure automatically (see apply_framerate). The
# ``exposure`` value below is the *budget* upper bound: the brightest single
# frame we aim for when the frame period allows it.
cam_controls = {
    "red_gain": 2.4,
    "green_gain": 1.0,   # software-only (picamera2 ColourGains has R, B only)
    "blue_gain": 2.5,
    "framerate": STREAM_FPS,
    "exposure": 20000,    # computed/clamped by apply_framerate()
    "analogue_gain": 1.0,
    "colour_gain": 1.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "brightness": 0.0,
    "sharpness": 1.0,
}

# The brightness target ("exposure budget") in units of exposure_us * gain.
# When the frame rate forces the exposure down, the analogue gain is raised to
# keep this product constant so the image does not get darker. Seeded from the
# defaults above.
exposure_budget = cam_controls["exposure"] * cam_controls["analogue_gain"]

# ========== Recording state ==========
is_recording = False
current_recording_filename = None
recording_started_at = None
recording_duration = None  # seconds, or None for open-ended

_stop_recording_event = threading.Event()


def apply_camera_controls():
    """Apply hardware camera parameters to picamera2."""
    if picam2 is None:
        return
    cg = 1.0
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


def apply_framerate(fps):
    """Set the capture frame rate and derive the exposure/gain that makes it
    achievable while keeping the image brightness constant.

    Physics: a frame lasts ``1/fps`` seconds, so the exposure can be at most
    that long (we keep a safety margin). When a higher frame rate forces the
    exposure below the brightness budget, the analogue gain is scaled up by the
    same factor so the picture does not get darker - this is what fixes the
    "goes dark above 30 fps" problem: the program now compensates automatically.
    """
    global exposure_budget
    fps = max(MIN_FPS, min(MAX_FPS, float(fps)))
    cam_controls["framerate"] = fps

    # Longest exposure that fits inside one frame, with a margin for readout.
    frame_period_us = 1_000_000.0 / fps
    max_exposure_us = int(frame_period_us * 0.92)

    # Use the budget's worth of light, but never longer than the frame allows.
    exposure_us = min(exposure_budget, max_exposure_us)
    exposure_us = max(100, int(exposure_us))

    # Whatever brightness the exposure could not provide, recover with gain.
    analogue_gain = exposure_budget / exposure_us
    analogue_gain = max(1.0, min(16.0, analogue_gain))

    cam_controls["exposure"] = exposure_us
    cam_controls["analogue_gain"] = round(analogue_gain, 3)

    with camera_lock:
        if picam2 is not None:
            picam2.set_controls({
                "FrameRate": fps,
                "ExposureTime": exposure_us,
                "AnalogueGain": cam_controls["analogue_gain"],
            })


def set_exposure_budget_from_gain(analogue_gain):
    """Treat a manual analogue-gain change as a brightness (budget) change.

    The exposure itself is locked to the frame rate, so the only free knob the
    user has for brightness is the analogue gain. When they move it we fold the
    new brightness into the budget and re-derive the operating point so the
    relationship survives the next frame-rate change.
    """
    global exposure_budget
    analogue_gain = max(1.0, min(16.0, float(analogue_gain)))
    exposure_budget = cam_controls["exposure"] * analogue_gain
    apply_framerate(cam_controls["framerate"])


# ========== Camera startup ==========
def _start_camera_unlocked():
    """(Re)start the camera. Must be called while holding camera_lock (RLock,
    so nested calls from the same thread are safe)."""
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
        controls={"FrameRate": cam_controls["framerate"]},
    )
    picam2.configure(config)
    picam2.start()
    camera_running = True
    time.sleep(0.5)
    apply_framerate(cam_controls["framerate"])
    apply_camera_controls()
    print(f"Camera started: {cam_controls['framerate']} fps, "
          f"exposure {cam_controls['exposure']}us, gain {cam_controls['analogue_gain']}")


def start_camera():
    """Public interface: acquire camera_lock then start the camera."""
    with camera_lock:
        _start_camera_unlocked()


# ========== Live display worker ==========
def _yuv_to_bgr(frame_yuv):
    return cv2.cvtColor(frame_yuv, cv2.COLOR_YUV2BGR_I420)


def display_worker():
    """Continuously capture the main stream, convert to BGR, and publish a JPEG for the /video_feed route."""
    global current_jpeg, measured_fps
    frame_interval = 1.0 / DISPLAY_FPS
    next_frame_time = time.perf_counter()
    last_meta_time = 0.0

    while True:
        now = time.perf_counter()
        if now < next_frame_time:
            time.sleep(next_frame_time - now)
        next_frame_time = time.perf_counter() + frame_interval

        if not camera_running or picam2 is None:
            time.sleep(0.05)
            continue
        if calibration_running:
            time.sleep(0.05)
            continue

        try:
            with camera_use_lock:
                if calibration_running:
                    continue
                with camera_lock:
                    frame_yuv = picam2.capture_array("main")
                    # Sample the true frame duration roughly twice a second.
                    if time.time() - last_meta_time >= 0.5:
                        last_meta_time = time.time()
                        try:
                            md = picam2.capture_metadata()
                            dur = md.get("FrameDuration")
                            if dur:
                                measured_fps = round(1_000_000.0 / dur, 1)
                        except Exception:
                            pass
            if frame_yuv is None:
                continue

            frame = _yuv_to_bgr(frame_yuv)
         
            jpeg = encode_jpeg(frame, quality=70, colorspace="BGR")
            with _jpeg_lock:
                current_jpeg = jpeg
        except Exception as e:
            print(f"Display worker error: {e}")
            time.sleep(0.1)


# ========== Calibration helpers ==========
def _try_acquire_calibration() -> bool:
    """Atomic check-and-set of calibration_running. Returns True on acquire."""
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


def run_white_balance_thread(colour_gain):
    """
    Calibrate the red/blue colour gains using the camera's hardware AWB.

    The calibration routine (run_white_balance) loops for up to ~8 seconds
    while reading metadata. During that time display_worker is paused by the
    calibration_running flag, so we do NOT hold camera_lock or camera_use_lock
    during the loop. This prevents the video feed from freezing.
    """
    try:
        # 1. Run the calibration WITHOUT holding any camera locks.
        #    calibration_running == True ensures display_worker skips capture.

        gains = run_white_balance(picam2, current_colour_gain=colour_gain)
        if gains is not None and None not in gains:
            red_gain, blue_gain = gains
            cam_controls["red_gain"] = red_gain
            cam_controls["blue_gain"] = blue_gain
            with camera_lock:
                apply_camera_controls()
            print(f"White balance: red_gain={red_gain}, blue_gain={blue_gain}")
        else:
            print("White balance: 게인 계산 실패 — 기존 설정 유지")
            # 2. Update the control dictionary.
            cam_controls["red_gain"] = red_gain
            cam_controls["blue_gain"] = blue_gain
            # Do NOT change colour_gain – the compensation was already applied
            # inside run_white_balance.

            # 3. Apply the new gains to the hardware (brief lock only).
            with camera_lock:
                apply_camera_controls()

            print(f"White balance: red_gain={red_gain}, blue_gain={blue_gain}")
        else:
            print("White balance: camera did not report colour gains")
    except Exception as e:
        print(f"White balance error: {e}")
    finally:
        # 4. Always release the calibration flag so display_worker resumes.
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
        m = re.match(r"recording_(\d+)_", f)
        if m:
            numbers.append(int(m.group(1)))
    return (max(numbers) + 1) if numbers else 1


def start_recording_async(duration_sec, output_path):
    """Record the main stream to ``output_path`` for ``duration_sec`` seconds.

    ``duration_sec`` of ``None`` (or 0) records until ``_stop_recording_event``
    is set by the /stop_recording route. The capture frame rate and exposure
    are whatever the live settings already are (driven by the FPS slider), so
    no per-recording frame-rate juggling is needed.
    """
    global is_recording, current_recording_filename
    global recording_started_at, recording_duration

    current_recording_filename = os.path.splitext(os.path.basename(output_path))[0]
    is_recording = True
    recording_started_at = time.time()
    recording_duration = duration_sec if duration_sec else None
    _stop_recording_event.clear()

    with camera_lock:
        if picam2 is None:
            _start_camera_unlocked()
        encoder = H264Encoder(bitrate=10_000_000)
        output = FfmpegOutput(output_path)
        picam2.start_encoder(encoder, output)

    # Cancellable wait. timeout=None blocks until the stop event is set.
    _stop_recording_event.wait(timeout=duration_sec if duration_sec else None)

    with camera_lock:
        try:
            picam2.stop_encoder()
        except Exception as e:
            print(f"stop_encoder error: {e}")

    # Rename the file so its name reflects the *actual* length recorded, not
    # the planned duration (e.g. a 10-minute recording stopped after 3s is
    # saved as ..._3s.mp4, not ..._600s.mp4).
    elapsed = max(1, int(round(time.time() - recording_started_at)))
    saved_path = output_path
    try:
        new_path = re.sub(r"_(\d+s|inf)\.mp4$", f"_{elapsed}s.mp4", output_path)
        if new_path != output_path and os.path.exists(output_path):
            os.rename(output_path, new_path)
            saved_path = new_path
    except OSError as e:
        print(f"Could not rename recording to actual length: {e}")

    is_recording = False
    recording_started_at = None
    recording_duration = None
    current_recording_filename = os.path.splitext(os.path.basename(saved_path))[0]
    print(f"Recording saved: {saved_path}")
