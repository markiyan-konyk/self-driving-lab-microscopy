#!/usr/bin/env python3
"""Offline check of the UI's client-side recorder. No microscope, no gateway.

    python ui/test_run_ui.py        (or: pytest)

It covers the two things about recording that are silently wrong rather than
loudly broken: a clip whose frame count does not match the footage, and a second
recording started while the first is still closing.
"""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("SCOPIO_API_KEY", "offline-test")
import run_ui  # noqa: E402


def _feed(frames, gap=0.01):
    """Push `frames` distinct JPEGs into the shared state, like the ingest loop."""
    jpeg = _jpeg()
    for _ in range(frames):
        with run_ui.state.lock:
            run_ui.state.jpeg = jpeg
            run_ui.state.jpeg_seq += 1
        time.sleep(gap)


def _jpeg():
    import cv2
    import numpy as np
    ok, buf = cv2.imencode(".jpg", np.full((48, 64, 3), 120, np.uint8))
    assert ok
    return buf.tobytes()


def test_one_written_frame_per_ingested_frame():
    """The clip's frame count IS its timebase. Sampling state.jpeg on a timer
    instead wrote the same frame twice whenever the camera ran slower than the
    declared fps, which reads downstream as beads that stopped moving."""
    import cv2

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "recording_1_10fps_5s.mp4")
        run_ui._rec["stop"].clear()
        with run_ui.state.lock:
            run_ui.state.jpeg = None        # start from an empty stream
        # Declared 10 fps, frames arriving at 50: a timer-driven writer would
        # write ~10 of the 12 and a stalled one would write the same frame twice.
        writer = threading.Thread(target=run_ui._record_loop, args=(path, 10, 5.0))
        writer.start()
        time.sleep(0.15)                    # let the writer reach its poll loop
        _feed(12, gap=0.02)
        time.sleep(0.2)
        run_ui._rec["stop"].set()
        writer.join(timeout=10)

        saved = [f for f in os.listdir(tmp) if f.endswith(".mp4")]
        assert len(saved) == 1, saved
        cap = cv2.VideoCapture(os.path.join(tmp, saved[0]))
        written = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        assert written == 12, f"12 frames in, {written} frames out"
        # ...and the name carries the length it really ran, not the requested one.
        assert saved[0] != "recording_1_10fps_5s.mp4", saved[0]


def test_a_second_recording_cannot_start_over_the_first():
    """`active` goes False before the writer has closed the file, so a quick
    stop-then-start ran two writers -- and the older one, finishing later,
    cleared `active` under the newer recording."""
    app = run_ui.app.test_client()
    with app.session_transaction() as s:
        s["authed"] = True

    with tempfile.TemporaryDirectory() as tmp:
        run_ui.recordings_dir = tmp
        with run_ui.state.lock:
            run_ui.state.jpeg = _jpeg()
            run_ui.state.jpeg_seq += 1
        run_ui.record_duration = None

        assert app.post("/start_recording").status_code == 200
        assert app.post("/start_recording").status_code == 409, "flag interlock"
        app.post("/stop_recording")
        # Immediately, before the writer thread has necessarily finished.
        assert app.post("/start_recording").status_code == 200
        assert run_ui._rec["thread"].is_alive()
        app.post("/stop_recording")
        run_ui._rec["thread"].join(timeout=10)
        assert run_ui._rec["active"] is False
        assert len([f for f in os.listdir(tmp) if f.endswith(".mp4")]) == 2


def test_each_jog_button_pans_the_way_it_is_labelled():
    """Measured on the rig: +x pans the picture LEFT and +y pans it UP (the
    camera is mounted turned relative to the stage). Every button must therefore
    command the delta that produces the direction it is named after -- this was
    a quarter turn out, so "up" panned left and "right" panned up."""
    picture = {(1, 0): "left", (-1, 0): "right", (0, 1): "up", (0, -1): "down"}
    steps = {"x": 40, "y": 40, "z": 40}
    for button in ("up", "down", "left", "right"):
        dx, dy, dz = run_ui.dir_delta(button, steps)
        assert dz == 0, f"{button} must not touch focus"
        moved = picture[(0 if not dx else dx // abs(dx), 0 if not dy else dy // abs(dy))]
        assert moved == button, f"pressing {button} pans {moved}"
    # Focus is the other axis entirely, and is not part of the rotation.
    assert run_ui.dir_delta("page_up", steps) == (0, 0, 40)
    assert run_ui.dir_delta("page_down", steps) == (0, 0, -40)
    assert run_ui.dir_delta("sideways", steps) is None


def test_the_recording_folder_is_validated():
    app = run_ui.app.test_client()
    with app.session_transaction() as s:
        s["authed"] = True

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "new", "nested")
        r = app.post("/recordings/dir", json={"path": target})
        assert r.status_code == 200 and os.path.isdir(target)
        assert r.get_json()["path"] == target
        assert app.post("/recordings/dir", json={"path": ""}).status_code == 400
        # A path that cannot be created is refused, and the old one is kept.
        bad = os.path.join(tmp, "file")
        open(bad, "w").close()
        assert app.post("/recordings/dir", json={"path": bad}).status_code == 400
        assert run_ui.recordings_dir == target


def test_a_screenshot_lands_in_the_clip_folder():
    """The browser sends the pixels, so what is saved is exactly what was on
    screen -- frozen frame and burnt-in scale bar included."""
    app = run_ui.app.test_client()
    with app.session_transaction() as s:
        s["authed"] = True

    with tempfile.TemporaryDirectory() as tmp:
        run_ui.recordings_dir = tmp
        shot = app.post("/screenshot", data=_jpeg(), content_type="image/jpeg")
        assert shot.status_code == 200
        name = shot.get_json()["filename"]
        assert name.endswith(".jpg") and os.path.exists(os.path.join(tmp, name))
        # Two in the same second must not overwrite each other.
        second = app.post("/screenshot", data=_jpeg(), content_type="image/jpeg")
        assert second.get_json()["filename"] != name
        assert len(os.listdir(tmp)) == 2
        # Anything that is not a JPEG is refused rather than written.
        assert app.post("/screenshot", data=b"<html>",
                        content_type="image/jpeg").status_code == 400


def test_a_scale_is_refused_without_the_width_it_was_measured_at():
    """um_per_px alone is unconvertible: switch sensor mode and the same slide
    lands on a different number of pixels. The width has to travel with it."""
    app = run_ui.app.test_client()
    with app.session_transaction() as s:
        s["authed"] = True
    body = {"pixels": 320, "micrometres": 100}
    assert app.post("/set_calibration", json=body).status_code == 400
    assert app.post("/set_calibration", json={**body, "width": 0}).status_code == 400
    assert app.post("/set_calibration", json={"width": 640}).status_code == 400


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("\nall good")


if __name__ == "__main__":
    main()
