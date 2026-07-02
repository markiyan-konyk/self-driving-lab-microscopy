#!/usr/bin/env python3
"""galvo_draw - draw with the laser.

A standalone app (its own ROS client + tiny web canvas) that lets you draw a
shape -- or spin a wireframe cube -- and have the galvo trace it with the laser
as a continuous vector image. It talks to the SCOPIO backend ONLY through the
`awg/write` passthrough, so it's just another client (like the UI).

  browser canvas ──path──► this app ──arb-waveform SCPI──► /scopio/awg/write
                                                            (AWG loops it in HW)

Because the laser cannot be blanked, the path is always a single continuous
loop; separate strokes are joined by (visible) travel lines -- that's the nature
of a vector laser display. Amplitude (Vpp) scales the size.

Run (after sourcing ROS 2 + scopio_interfaces), or via docker compose:
    python3 app.py                      # http://localhost:8090
Env: GALVO_DRAW_PORT (8090), SCOPIO_NAMESPACE (/scopio).
"""

import os
import threading
import time

import numpy as np
import rclpy
from rclpy.node import Node

from scopio_interfaces.srv import AwgWrite
from flask import Flask, request, jsonify, send_from_directory

from galvo_scpi import (
    arb_upload_cmds, arb_apply_cmds, phase_sync_cmds, park_cmds, test_point_cmds,
)

HERE = os.path.dirname(os.path.abspath(__file__))
NS = os.environ.get("SCOPIO_NAMESPACE", "/scopio").rstrip("/")
PORT = int(os.environ.get("GALVO_DRAW_PORT", 8090))
N_POINTS = 2048          # samples uploaded per channel (within the AWG's memory)


class AwgClient(Node):
    def __init__(self):
        super().__init__("galvo_draw")
        self.cli = self.create_client(AwgWrite, f"{NS}/awg/write")

    def write(self, command, timeout=4.0):
        if not self.cli.wait_for_service(timeout_sec=1.0):
            raise RuntimeError("awg/write unavailable (is the backend running?)")
        fut = self.cli.call_async(AwgWrite.Request(command=command))
        done = threading.Event()
        fut.add_done_callback(lambda _f: done.set())
        if not done.wait(timeout):
            raise RuntimeError("awg/write timed out")
        res = fut.result()
        if not res.success:
            raise RuntimeError(res.error or "awg/write failed")

    def send(self, cmds):
        for c in cmds:
            self.write(c)


# ---- shared desired state (written by Flask, consumed by the sender thread) ----
state = {"x": None, "y": None, "vpp": 1.0, "freq": 60.0, "enabled": False, "version": 0}
_applied = -1
_lock = threading.Lock()
_wake = threading.Event()
_status = {"connected": False, "message": "idle"}
node = None


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
                node.send(park_cmds(0.0, 0.0))         # hold a dot
                _status.update(connected=True, message="parked (dot)")
            else:
                node.send(arb_upload_cmds(1, x))
                node.send(arb_upload_cmds(2, y))
                node.send(arb_apply_cmds(1, vpp, freq) +
                          arb_apply_cmds(2, vpp, freq) +
                          phase_sync_cmds())
                _status.update(connected=True, message=f"drawing @ {freq:.0f} Hz, {vpp:.2f} Vpp")
            _applied = ver
        except Exception as e:
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
    """Known-good link check: a static DC offset (independent of arb upload)."""
    try:
        node.send(test_point_cmds(0.5, 0.0))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/status")
def status():
    return jsonify(_status)


def main():
    global node
    rclpy.init()
    node = AwgClient()
    threading.Thread(target=lambda: rclpy.spin(node), daemon=True).start()
    threading.Thread(target=sender_loop, daemon=True).start()
    node.get_logger().info(f"galvo_draw on http://0.0.0.0:{PORT} (ns {NS})")
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
