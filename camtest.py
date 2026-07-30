#!/usr/bin/env python3
"""camtest.py -- does the IMX296 on the 22-pin CSI port actually work?

RUN THIS ON THE RASPBERRY PI (Raspberry Pi OS Bookworm, python3-picamera2
installed). It is a standalone diagnostic -- it imports nothing from this repo
and touches no ROS, no gateway, no camera server. Stop the camera server /
container first: the sensor has exactly one owner at a time.

    python3 camtest.py                 # full check, saves a test frame
    python3 camtest.py --list          # just enumerate what libcamera sees
    python3 camtest.py --web           # + serve live MJPEG at http://<pi>:8088/
    python3 camtest.py --preview       # + on-screen preview (needs a desktop)

What it checks, in order, stopping at the first hard failure:
  1  environment   -- picamera2 imports, libcamera present, rpicam tools
  2  wiring        -- is a camera enumerated, is it an imx296, which CSI port
  3  config.txt    -- camera_auto_detect / dtoverlay lines, imx296 driver probe
  4  configure     -- open the sensor, list its native modes
  5  capture       -- pull frames, measure real fps from FrameDuration
  6  pixels        -- mean/stddev/min/max: is there an IMAGE or just a flat wall
  7  display       -- save a JPEG/PNG, and optionally preview or stream it

The IMX296 is the Raspberry Pi Global Shutter Camera: 1456x1088, and it comes
in colour AND mono variants (both report "imx296"). Which one you have is
detected from the sensor's raw format and reported -- a mono sensor producing
grey frames is a PASS, not a fault.

Exit code 0 = the camera works and produced a real image. Non-zero = the
failing stage is named in the summary.
"""

import argparse
import os
import subprocess
import sys
import time

# Every check appends (name, ok, detail) here; printed as a summary at the end.
results = []
CONFIG_PATHS = ("/boot/firmware/config.txt", "/boot/config.txt")


def say(msg=""):
    print(msg, flush=True)


def stage(n, title):
    say()
    say(f"[{n}] {title}")
    say("-" * (len(title) + 4))


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    say(f"    {'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")
    return ok


def die(stage_name, detail, hint=""):
    """Hard stop: this failure makes every later stage meaningless."""
    record(stage_name, False, detail)
    if hint:
        say()
        say("  TRY THIS:")
        for line in hint.strip().splitlines():
            say(f"    {line.strip()}")
    summary()
    sys.exit(1)


def run(cmd, timeout=15):
    """Run a shell command, return (rc, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as e:                                  # noqa: BLE001
        return 1, str(e)


# ---------------------------------------------------------------------- #
#  1  Environment
# ---------------------------------------------------------------------- #
def check_environment():
    stage(1, "Environment")
    say(f"    python      {sys.version.split()[0]}  ({sys.executable})")

    model = "unknown"
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\x00").strip()
    except OSError:
        pass
    say(f"    board       {model}")
    if "Raspberry Pi" not in model:
        die("running on a Pi", f"board reports {model!r}",
            "This script only works on the Pi itself -- copy it over and run it there:\n"
            "scp camtest.py pi@<pi-host>:~ && ssh pi@<pi-host> python3 camtest.py")

    try:
        from picamera2 import Picamera2
    except Exception as e:                                  # noqa: BLE001
        die("import picamera2", str(e),
            "sudo apt update && sudo apt install -y python3-picamera2\n"
            "Run with the SYSTEM python3 (no venv, or create it with "
            "--system-site-packages).\n"
            "picamera2 is Raspberry Pi OS only -- it cannot run inside the "
            "Ubuntu ROS container.")
    record("import picamera2", True, getattr(Picamera2, "__module__", "ok"))

    try:
        import libcamera
        say(f"    libcamera   {getattr(libcamera, '__version__', 'present')}")
    except Exception as e:                                  # noqa: BLE001
        say(f"    libcamera   python bindings missing ({e})")

    rc, out = run("rpicam-hello --list-cameras 2>&1 || libcamera-hello --list-cameras 2>&1")
    if rc == 0 and out.strip():
        say("    rpicam-hello --list-cameras:")
        for line in out.strip().splitlines()[:24]:
            say(f"      | {line}")
    else:
        say("    rpicam-hello  unavailable (not fatal; picamera2 is what we use)")
    return Picamera2


# ---------------------------------------------------------------------- #
#  2  Wiring / enumeration
# ---------------------------------------------------------------------- #
def check_enumeration(Picamera2, want_index):
    stage(2, "Camera enumeration (is the ribbon seated and detected?)")
    try:
        cams = Picamera2.global_camera_info()
    except Exception as e:                                  # noqa: BLE001
        die("libcamera enumeration", str(e))

    if not cams:
        check_boot_config()                 # print the boot clues before dying
        die("camera detected", "libcamera sees ZERO cameras",
            "Power OFF the Pi before touching the ribbon (CSI is not hot-plug).\n"
            "22-pin FFC: contacts face the board -- blue tab faces the USB/"
            "ethernet side on the Pi, and the silver contacts face the PCB on\n"
            "the camera end. A cable in backwards or half-latched enumerates as "
            "nothing at all.\n"
            "On a Pi 5 use either CAM/DISP port (cam0 or cam1); on a Pi 4 the "
            "single 15-pin port needs a 22-to-15-pin adapter cable.\n"
            "Check /boot/firmware/config.txt has camera_auto_detect=1 (or "
            "dtoverlay=imx296), then reboot.")

    for i, c in enumerate(cams):
        mark = "->" if i == (want_index or 0) else "  "
        say(f"  {mark} [{i}] model={c.get('Model')}  id={c.get('Id')}")
        if c.get("Location") is not None:
            say(f"        location={c.get('Location')} rotation={c.get('Rotation')}")
    record("camera detected", True, f"{len(cams)} camera(s)")

    index = want_index
    if index is None:
        index = next((i for i, c in enumerate(cams)
                      if "imx296" in str(c.get("Model", "")).lower()), 0)
    if index >= len(cams):
        die("camera index", f"--camera {index} but only {len(cams)} present")

    cam = cams[index]
    model = str(cam.get("Model", "")).lower()
    cam_id = str(cam.get("Id", ""))
    if "imx296" in model:
        record("sensor is IMX296", True, f"index {index}")
    else:
        # Not fatal: keep testing so the user learns what IS plugged in.
        record("sensor is IMX296", False,
               f"index {index} reports {cam.get('Model')!r} -- testing it anyway")

    # The 22-pin CSI ports appear as distinct i2c buses in the device path.
    port = "unknown"
    if "i2c@88000" in cam_id or "csi2@88000" in cam_id:
        port = "CAM1 (22-pin)"
    elif "i2c@80000" in cam_id or "csi2@80000" in cam_id:
        port = "CAM0 (22-pin)"
    elif "i2c@7e804000" in cam_id or "i2c@0" in cam_id:
        port = "legacy 15-pin CSI (via adapter)"
    say(f"    CSI port    {port}")
    return index


# ---------------------------------------------------------------------- #
#  3  Boot config + kernel probe (informational, never fatal)
# ---------------------------------------------------------------------- #
def check_boot_config():
    stage(3, "Boot config and kernel driver probe")
    path = next((p for p in CONFIG_PATHS if os.path.exists(p)), None)
    if path:
        say(f"    {path}:")
        hits = []
        try:
            with open(path) as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    if any(k in s for k in ("camera_auto_detect", "dtoverlay=imx",
                                            "start_x", "gpu_mem", "cam0", "cam1",
                                            "dtoverlay=vc4")):
                        hits.append(s)
        except OSError as e:
            say(f"      (unreadable: {e})")
        for h in hits or ["(no camera-related lines -- auto-detect defaults apply)"]:
            say(f"      | {h}")
    else:
        say("    config.txt not found (looked in /boot/firmware and /boot)")

    rc, out = run("dmesg 2>/dev/null | grep -i -e imx296 -e unicam -e rp1-cfe | tail -n 12")
    if out.strip():
        say("    dmesg (imx296 / CSI receiver):")
        for line in out.strip().splitlines():
            say(f"      | {line}")
    else:
        say("    dmesg  no imx296/CSI lines (run with sudo to read the ring buffer)")


# ---------------------------------------------------------------------- #
#  4  Configure the sensor
# ---------------------------------------------------------------------- #
def configure(Picamera2, index, width, height, fps):
    stage(4, "Open and configure the sensor")
    try:
        picam2 = Picamera2(index)
    except Exception as e:                                  # noqa: BLE001
        die("open sensor", str(e),
            "Something else already owns the camera. Stop it first:\n"
            "sudo systemctl stop scopio-camera   # the camera server unit\n"
            "docker compose -f ros2_ws/docker-compose.yml stop camera\n"
            "pgrep -a -f 'rpicam|libcamera|pi_camera_server|camera_node'")

    mono = False
    try:
        modes = picam2.sensor_modes
        say(f"    native sensor modes ({len(modes)}):")
        for m in modes:
            say(f"      | size={m.get('size')} format={m.get('format')} "
                f"bit_depth={m.get('bit_depth')} fps<={m.get('fps')}")
        raw_fmt = str(modes[0].get("format", "")) if modes else ""
        # Colour sensors expose a Bayer raw format (SBGGR/SRGGB/SGBRG/SGRBG);
        # the mono IMX296 exposes plain R8/R10/R12.
        mono = bool(raw_fmt) and not any(b in raw_fmt.upper() for b in
                                         ("BGGR", "RGGB", "GBRG", "GRBG"))
        record("sensor modes readable", True,
               f"{'MONO' if mono else 'COLOUR'} variant (raw {raw_fmt or '?'})")
    except Exception as e:                                  # noqa: BLE001
        record("sensor modes readable", False, str(e))

    # RGB888 keeps this test independent of any YUV conversion: whatever the
    # ISP hands us is directly inspectable and directly saveable.
    try:
        cfg = picam2.create_still_configuration(main={"size": (width, height),
                                                      "format": "RGB888"})
        picam2.configure(cfg)
        applied = picam2.camera_configuration()["main"]
        record("configure", True, f"{applied['size']} {applied['format']}")
        if tuple(applied["size"]) != (width, height):
            say(f"    NOTE libcamera adjusted the size to {applied['size']}")
    except Exception as e:                                  # noqa: BLE001
        die("configure", str(e))

    try:
        picam2.start()
    except Exception as e:                                  # noqa: BLE001
        die("start streaming", str(e),
            "A start failure with a healthy enumeration usually means a marginal "
            "ribbon (reseat both ends) or an underpowered supply.")
    record("start streaming", True)

    # Auto exposure/AWB deliberately left ON: this test asks "is there an
    # image", not "is it the exposure the microscope wants".
    try:
        picam2.set_controls({"FrameRate": float(fps), "AeEnable": True,
                             "AwbEnable": not mono})
    except Exception as e:                                  # noqa: BLE001
        say(f"    NOTE could not set FrameRate/AE ({e})")
    time.sleep(1.0)                       # let AE/AWB settle before judging pixels
    return picam2, mono


# ---------------------------------------------------------------------- #
#  5 + 6  Capture, timing, pixel sanity
# ---------------------------------------------------------------------- #
def check_capture(picam2, n_frames, target_fps):
    stage(5, f"Capture {n_frames} frames and measure real frame rate")
    try:
        import numpy as np
    except Exception as e:                                  # noqa: BLE001
        die("import numpy", str(e), "sudo apt install -y python3-numpy")

    frames, durations = [], []
    t0 = time.time()
    for i in range(n_frames):
        try:
            req = picam2.capture_request()
        except Exception as e:                              # noqa: BLE001
            die("capture frame", f"frame {i}: {e}")
        try:
            arr = req.make_array("main").copy()
            meta = req.get_metadata()
        finally:
            req.release()
        frames.append(arr)
        d = meta.get("FrameDuration")
        if d:
            durations.append(d)
    wall = time.time() - t0

    shape = frames[0].shape
    record("capture frames", True, f"{len(frames)} x {shape} in {wall:.2f}s")
    wall_fps = len(frames) / wall if wall > 0 else 0.0
    sensor_fps = (1_000_000.0 / (sum(durations) / len(durations))) if durations else 0.0
    say(f"    requested   {target_fps:.1f} fps")
    say(f"    sensor      {sensor_fps:.1f} fps  (from FrameDuration metadata)")
    say(f"    wall clock  {wall_fps:.1f} fps  (includes this script's own copies)")
    record("frame rate reported", sensor_fps > 0,
           f"{sensor_fps:.1f} fps" if sensor_fps else "no FrameDuration metadata")

    last = frames[-1]
    m = last.astype("float32")
    mean, std, lo, hi = float(m.mean()), float(m.std()), int(last.min()), int(last.max())

    stage(6, "Pixel sanity (is this an image, or a flat wall?)")
    say(f"    dtype={last.dtype} shape={shape}")
    say(f"    mean={mean:.1f}  stddev={std:.1f}  min={lo}  max={hi}")
    exp = picam2.capture_metadata()
    say(f"    ExposureTime={exp.get('ExposureTime')} us  "
        f"AnalogueGain={exp.get('AnalogueGain')}  "
        f"ColourGains={exp.get('ColourGains')}")

    ok = record("frame has variation", std > 1.0,
                f"stddev {std:.2f} -- a truly flat frame means no light path or a "
                "dead stream" if std <= 1.0 else f"stddev {std:.2f}")
    if mean < 3.0:
        record("frame is not black", False,
               f"mean {mean:.1f} -- lens cap on, no illumination, or exposure far "
               "too short")
        ok = False
    elif mean > 250.0:
        record("frame is not blown out", False,
               f"mean {mean:.1f} -- fully saturated; dim the illumination")
        ok = False
    else:
        record("exposure is plausible", True, f"mean {mean:.1f}")

    if last.ndim == 3 and last.shape[2] >= 3:
        ch = [float(last[:, :, c].mean()) for c in range(3)]
        say(f"    channel means B={ch[0]:.1f} G={ch[1]:.1f} R={ch[2]:.1f}")
        if max(ch) - min(ch) < 1.0:
            say("    channels identical -> MONO sensor (expected for the mono GS camera)")

    try:
        import cv2
        gray = cv2.cvtColor(last, cv2.COLOR_BGR2GRAY) if last.ndim == 3 else last
        say(f"    focus (variance of Laplacian) = "
            f"{float(cv2.Laplacian(gray, cv2.CV_64F).var()):.1f}  (higher = sharper)")
    except Exception:                                       # noqa: BLE001
        pass
    return ok, frames[-1]


# ---------------------------------------------------------------------- #
#  7  Display: save, preview, stream
# ---------------------------------------------------------------------- #
def save_frame(picam2, frame, out_path):
    stage(7, "Display / save")
    saved = []
    try:
        picam2.capture_file(out_path)         # ISP-encoded, correct colour
        saved.append(out_path)
    except Exception as e:                                  # noqa: BLE001
        say(f"    capture_file failed ({e}); falling back to a raw dump")
    if not saved:
        try:
            import cv2
            alt = os.path.splitext(out_path)[0] + ".png"
            cv2.imwrite(alt, frame)
            saved.append(alt)
        except Exception as e:                              # noqa: BLE001
            record("save image", False, str(e))
            return
    for p in saved:
        size = os.path.getsize(p) if os.path.exists(p) else 0
        record("save image", size > 0, f"{os.path.abspath(p)} ({size} bytes)")
    say()
    say("    View it:")
    say(f"      on the Pi desktop:  xdg-open {os.path.abspath(saved[0])}")
    say(f"      from your machine:  scp {os.uname().nodename}:{os.path.abspath(saved[0])} .")


def on_screen_preview(picam2, seconds):
    """Real on-screen preview. Needs a desktop session (or a KMS console)."""
    say()
    say(f"[+] On-screen preview for {seconds:.0f}s ...")
    from picamera2 import Preview
    for name in ("QTGL", "QT", "DRM"):
        try:
            picam2.start_preview(getattr(Preview, name))
            record(f"preview ({name})", True, f"showing for {seconds:.0f}s")
            time.sleep(seconds)
            picam2.stop_preview()
            return True
        except Exception as e:                              # noqa: BLE001
            say(f"    {name} preview unavailable: {e}")
    record("preview", False,
           "no display available -- use --web and watch it in a browser instead")
    return False


def web_preview(Picamera2, index, width, height, fps, port):
    """Serve live MJPEG so a headless Pi can still be *seen*. Ctrl-C to stop."""
    import socketserver
    from http import server as http_server
    from threading import Condition
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput

    say()
    say("[+] Live MJPEG stream")

    class Sink(object):
        """Latest-JPEG holder; MJPEG frames arrive as whole buffers."""
        def __init__(self):
            self.frame = None
            self.cond = Condition()

        def write(self, buf):
            with self.cond:
                self.frame = buf
                self.cond.notify_all()

    sink = Sink()
    picam2 = Picamera2(index)
    picam2.configure(picam2.create_video_configuration(main={"size": (width, height)}))
    picam2.set_controls({"FrameRate": float(fps)})
    picam2.start_recording(MJPEGEncoder(), FileOutput(sink))

    PAGE = ("<!DOCTYPE html><html><head><title>camtest -- IMX296</title>"
            "<style>body{background:#111;color:#ddd;font:14px system-ui;"
            "text-align:center;margin:0;padding:1rem}img{max-width:100%;"
            "border:1px solid #2a2a2a}</style></head><body>"
            f"<h3>IMX296 live &mdash; {width}x{height} @ {fps:g} fps</h3>"
            "<img src='/stream.mjpg'></body></html>").encode()

    class Handler(http_server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(PAGE)))
                self.end_headers()
                self.wfile.write(PAGE)
            elif self.path == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=FRAME")
                self.end_headers()
                try:
                    while True:
                        with sink.cond:
                            sink.cond.wait()
                            frame = sink.frame
                        self.wfile.write(b"--FRAME\r\n")
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(frame)))
                        self.end_headers()
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                except Exception:                           # noqa: BLE001
                    pass                                    # client went away
            else:
                self.send_error(404)

    class Server(socketserver.ThreadingMixIn, http_server.HTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    host = os.uname().nodename
    say(f"    open  http://{host}:{port}/   (or http://<pi-ip>:{port}/)")
    say("    Ctrl-C to stop.")
    try:
        Server(("0.0.0.0", port), Handler).serve_forever()
    except KeyboardInterrupt:
        say("\n    stopped.")
    finally:
        picam2.stop_recording()
        picam2.close()


# ---------------------------------------------------------------------- #
def summary():
    say()
    say("=" * 62)
    say("SUMMARY")
    for name, ok, detail in results:
        say(f"  {'PASS' if ok else 'FAIL':4}  {name}{'  --  ' + detail if detail else ''}")
    bad = [n for n, ok, _ in results if not ok]
    say("=" * 62)
    if bad:
        say(f"RESULT: {len(bad)} check(s) failed: {', '.join(bad)}")
    else:
        say("RESULT: camera works -- frames captured and saved.")
    say()


def main():
    ap = argparse.ArgumentParser(description="Test an IMX296 (Pi Global Shutter "
                                            "Camera) on the 22-pin CSI port.")
    ap.add_argument("--camera", type=int, default=None,
                    help="camera index to test (default: auto-pick the imx296)")
    ap.add_argument("--width", type=int, default=1456, help="capture width (native 1456)")
    ap.add_argument("--height", type=int, default=1088, help="capture height (native 1088)")
    ap.add_argument("--fps", type=float, default=30.0, help="requested frame rate")
    ap.add_argument("--frames", type=int, default=10, help="frames to capture")
    ap.add_argument("--out", default="camtest.jpg", help="where to save the test frame")
    ap.add_argument("--list", action="store_true", help="only enumerate cameras, then exit")
    ap.add_argument("--preview", type=float, nargs="?", const=10.0, default=None,
                    metavar="SECONDS", help="show an on-screen preview (needs a desktop)")
    ap.add_argument("--web", action="store_true", help="serve live MJPEG after the checks")
    ap.add_argument("--port", type=int, default=8088, help="port for --web (default 8088)")
    args = ap.parse_args()

    say("=" * 62)
    say("camtest.py -- IMX296 / 22-pin CSI camera check")
    say("=" * 62)

    Picamera2 = check_environment()
    index = check_enumeration(Picamera2, args.camera)
    check_boot_config()
    if args.list:
        summary()
        return 0

    picam2, _mono = configure(Picamera2, index, args.width, args.height, args.fps)
    try:
        ok, frame = check_capture(picam2, max(1, args.frames), args.fps)
        save_frame(picam2, frame, args.out)
        if args.preview is not None:
            on_screen_preview(picam2, args.preview)
    finally:
        try:
            picam2.stop()
            picam2.close()
        except Exception:                                   # noqa: BLE001
            pass

    summary()
    if args.web:
        web_preview(Picamera2, index, args.width, args.height, args.fps, args.port)
    return 0 if (ok and not [n for n, o, _ in results if not o]) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\ninterrupted.")
        sys.exit(130)
