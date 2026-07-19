#!/usr/bin/env python3
"""galvo_draw - draw with the laser.

A standalone app (tiny web canvas + API client) that lets you draw a shape --
or spin a wireframe cube -- and have the galvo trace it with the laser as a
continuous vector image. It talks to the SCOPIO microscope ONLY through the
`awg/write` passthrough on the API gateway, so it's just another client (like
the UI): no ROS, no Docker, no DDS.

  browser canvas ──path──► this app ──arb-waveform SCPI──► gateway ──► awg/write
                                                            (AWG loops it in HW)

Because the laser cannot be blanked, the path is always a single continuous
loop; separate strokes are joined by (visible) travel lines -- that's the nature
of a vector laser display. Amplitude (Vpp) scales the size.

Run:
    pip install -r requirements.txt
    set SCOPIO_URL=http://<pi-ip>:8000
    set SCOPIO_API_KEY=<key from ros2_ws/scripts/generate_api_key.py>
    python app.py                       # http://localhost:8090

Env: SCOPIO_URL, SCOPIO_API_KEY (required); GALVO_DRAW_PORT (8090).
"""

import os
import threading
import time

import numpy as np
from flask import Flask, request, jsonify, send_from_directory

from scopio_client import Scopio, ScopioError
from galvo_scpi import (
    arb_upload_cmds, arb_apply_cmds, phase_sync_cmds, park_cmds, test_circle_cmds,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("GALVO_DRAW_PORT", 8090))
SCOPIO_URL = os.environ.get("SCOPIO_URL", "http://127.0.0.1:8000")
SCOPIO_API_KEY = os.environ.get("SCOPIO_API_KEY", "")
N_POINTS = 2048          # samples uploaded per channel (within the AWG's memory)

# ---- shared desired state (written by Flask, consumed by the sender thread) ----
state = {"x": None, "y": None, "vpp": 1.0, "freq": 60.0, "enabled": False, "version": 0}
_applied = -1
_lock = threading.Lock()
_wake = threading.Event()
_status = {"connected": False, "message": "idle"}
scope = None      # set in main()


def resample_closed(points, n=N_POINTS):
    """Resample an (M,2) path to n points evenly spaced by arc length, looping
    back to the start so the AWG waveform is seamless."""
    p = np.asarray(points, dtype=float)
    if len(p) < 2:
        return None
    p = np.vstack([p, p[0]])                       # close the loop
    seg = np.sqrt(((np.diff(p, axis=0)) ** 2).sum(1))
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] == 0:
        return None
    t = np.linspace(0, s[-1], n, endpoint=False)
    x = np.interp(t, s, p[:, 0])
    y = np.interp(t, s, p[:, 1])
    return x, y


def sender_loop():
    """Apply the latest desired drawing whenever it changes."""
    global _applied
    while True:
        _wake.wait(timeout=1.0)
        _wake.clear()
        with _lock:
            ver = state["version"]
            enabled = state["enabled"]
            x, y = state["x"], state["y"]
            vpp, freq = state["vpp"], state["freq"]
        if ver == _applied:
            continue
        try:
            if not enabled or x is None:
                scope.galvo.write_all(park_cmds(0.0, 0.0))     # hold a dot
                _status.update(connected=True, message="parked (dot)")
            else:
                scope.galvo.write_all(arb_upload_cmds(1, x))
                scope.galvo.write_all(arb_upload_cmds(2, y))
                scope.galvo.write_all(arb_apply_cmds(1, vpp, freq) +
                                      arb_apply_cmds(2, vpp, freq) +
                                      phase_sync_cmds())
                _status.update(connected=True, message=f"drawing @ {freq:.0f} Hz, {vpp:.2f} Vpp")
            _applied = ver
        except ScopioError as e:
            _status.update(connected=False, message=str(e))
            time.sleep(1.0)                            # back off, retry on next change


# ========== Flask ==========
app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "frontend"), "index.html")


@app.route("/set_path", methods=["POST"])
def set_path():
    d = request.get_json(force=True) or {}
    pts = d.get("points")
    with _lock:
        if pts and len(pts) >= 2:
            rs = resample_closed(pts)
            if rs is not None:
                state["x"], state["y"] = rs[0].tolist(), rs[1].tolist()
        if d.get("clear"):
            state["x"] = state["y"] = None
        state["vpp"] = max(0.0, min(10.0, float(d.get("vpp", state["vpp"]))))
        state["freq"] = max(5.0, min(500.0, float(d.get("freq", state["freq"]))))
        state["enabled"] = bool(d.get("enabled", state["enabled"]))
        state["version"] += 1
    _wake.set()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    with _lock:
        state["enabled"] = False
        state["version"] += 1
    _wake.set()
    return jsonify({"ok": True})


@app.route("/test", methods=["POST"])
def test():
    """Known-good link check using ONLY the proven sine path: draws a slow circle.
    If this works but a drawing doesn't, the fault is isolated to the arb-waveform
    path; if this fails too, the microscope isn't connected to the AWG (awg/status)."""
    try:
        scope.galvo.write_all(test_circle_cmds())
        return jsonify({"ok": True})
    except ScopioError as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/status")
def status():
    return jsonify(_status)


def main():
    global scope
    if not SCOPIO_API_KEY:
        raise SystemExit("Set SCOPIO_API_KEY (generate one on the Pi with "
                         "ros2_ws/scripts/generate_api_key.py) and SCOPIO_URL.")
    scope = Scopio(SCOPIO_URL, api_key=SCOPIO_API_KEY)
    threading.Thread(target=sender_loop, daemon=True).start()
    print(f"galvo_draw on http://0.0.0.0:{PORT} -> microscope {SCOPIO_URL}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
