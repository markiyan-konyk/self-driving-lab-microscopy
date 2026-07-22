#!/usr/bin/env python3
"""galvo_draw -- draw with the laser.

A standalone app (tiny web canvas + API client) that turns a drawing -- or a
draggable wireframe cube -- into laser motion: the galvo traces it as a single
continuous vector image. It talks to the SCOPIO microscope ONLY through the
``awg/write`` passthrough on the API gateway, so it's just another API client
(like ui/ and viscosity_agent/): no ROS, no Docker, no DDS, no pyvisa here.

  browser canvas --strokes--> this app --arb-waveform SCPI--> gateway --awg/write
                                                             (AWG loops it in HW)

The laser can't blank, so the path is always ONE continuous loop. By default the
connectors between separate strokes are traced FAST so they're faint ("dim
travel"), while the strokes are traced slowly so they're bright (see
galvo_paths.py). Amplitude (Vpp) scales the size; loop rate sets the refresh.

Run:
    pip install -r requirements.txt        # includes the scopio_client SDK
    cp .env.example .env                   # then put the Pi's URL + API key in it
    python app.py                          # -> http://localhost:8090

Config: connection in .env (SCOPIO_URL, SCOPIO_API_KEY); galvo knobs in
config.yaml. GALVO_DRAW_PORT overrides the port.
"""

import os
import threading
import time

import yaml
from flask import Flask, request, jsonify, send_from_directory

try:
    from dotenv import load_dotenv
except ImportError:                       # python-dotenv is optional (env still works)
    load_dotenv = None

from scopio_client import Scopio, ScopioError
from galvo_scpi import (
    arb_upload_cmds, arb_apply_cmds, phase_sync_cmds, park_cmds, test_circle_cmds,
)
from galvo_paths import build_loop

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- config: knobs from config.yaml, connection + port from .env / env ----
DEFAULTS = {
    "port": 8090,
    # Samples uploaded per channel. Kept SMALL on purpose: the ROS galvo_node
    # writes each ":DATA VOLATILE" over pyvisa/USB-TMC with a 5 s VISA timeout,
    # and a big ASCII arb command can't be ingested in time -> [Errno 110]. A few
    # hundred points is plenty (the DG1000Z interpolates to 8192 in HW anyway).
    "n_points": 256,           # 8..16384; lower this first if uploads time out
    "upload_settle_s": 0.25,   # pause after each channel's arb upload (flow ctrl)
    "write_timeout_s": 20.0,   # per-command gateway timeout for the big uploads
    "default_loop_hz": 45.0,   # waveform repetitions/sec (persistence-of-vision)
    "loop_hz_min": 20.0,
    "loop_hz_max": 100.0,
    "default_vpp": 1.0,        # amplitude == drawing size (Vpp)
    "max_vpp": 5.0,            # hard clamp on amplitude (galvo safe envelope)
    "dim_travel": True,        # faint fast connectors between strokes
    "travel_points": 3,        # samples per connector (fewer -> faster/fainter)
}


def load_config():
    if load_dotenv:
        load_dotenv(os.path.join(HERE, ".env"))
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "config.yaml")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    cfg["scopio_url"] = os.environ.get("SCOPIO_URL", "http://127.0.0.1:8000")
    cfg["scopio_api_key"] = os.environ.get("SCOPIO_API_KEY", "")
    cfg["port"] = int(os.environ.get("GALVO_DRAW_PORT", cfg["port"]))
    return cfg


CFG = load_config()

# ---- shared desired state (written by Flask, consumed by the sender thread) ----
state = {
    "strokes": None,           # list of strokes, each [[x,y],...] normalized [-1,1]
    "vpp": CFG["default_vpp"],
    "freq": CFG["default_loop_hz"],
    "dim_travel": CFG["dim_travel"],
    "enabled": False,          # laser output off until the user opts in (safety)
    "version": 0,
}
_applied = -1
_lock = threading.Lock()       # guards `state`
_awg_lock = threading.Lock()   # serializes AWG command batches (sender vs /test)
_wake = threading.Event()
_status = {"connected": False, "message": "idle"}
scope = None                   # set in main()


def _clampf(v, lo, hi, fallback):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return fallback


def _awg_write(cmd, timeout=None):
    """One SCPI command via the gateway, with an optional longer service timeout
    (the big arb uploads need more than the gateway's 10 s default)."""
    resp = scope.call_service("awg/write", {"command": cmd}, timeout=timeout)
    if not resp.get("success", False):
        raise ScopioError(f"awg/write failed: {resp.get('error')}")


def _send(cmds, timeout=None):
    """Send a SCPI batch atomically, so a /test never interleaves a drawing."""
    with _awg_lock:
        for c in cmds:
            _awg_write(c, timeout=timeout)


def _draw(x, y, vpp, freq):
    """Upload both channels' arbitrary waveforms, then apply. The two big uploads
    are spaced by a settle pause so the DG1022Z isn't still ingesting CH1's data
    when CH2's arrives (which stalls the USB-TMC transfer -> [Errno 110])."""
    settle, t = CFG["upload_settle_s"], CFG["write_timeout_s"]
    with _awg_lock:
        for c in arb_upload_cmds(1, x):
            _awg_write(c, timeout=t)
        time.sleep(settle)
        for c in arb_upload_cmds(2, y):
            _awg_write(c, timeout=t)
        time.sleep(settle)
        for c in (arb_apply_cmds(1, vpp, freq) + arb_apply_cmds(2, vpp, freq) +
                  phase_sync_cmds()):
            _awg_write(c)


def sender_loop():
    """Apply the latest desired drawing whenever it changes; coalesce rapid edits
    (only the newest version is ever applied)."""
    global _applied
    while True:
        _wake.wait(timeout=1.0)
        _wake.clear()
        with _lock:
            ver, enabled = state["version"], state["enabled"]
            strokes = state["strokes"]
            vpp, freq, dim = state["vpp"], state["freq"], state["dim_travel"]
        if ver == _applied:
            continue
        loop = (build_loop(strokes, n=CFG["n_points"], dim_travel=dim,
                           travel_pts=CFG["travel_points"])
                if enabled and strokes else None)
        try:
            if loop is None:
                _send(park_cmds(0.0, 0.0))          # hold a dot at centre
                _status.update(connected=True, message="parked (dot)")
            else:
                x, y = loop
                _draw(x, y, vpp, freq)
                _status.update(connected=True,
                               message=f"drawing {len(x)} pts @ {freq:.0f} Hz, "
                                       f"{vpp:.2f} Vpp"
                                       f"{', dim travel' if dim else ''}")
            _applied = ver
        except ScopioError as e:
            # Mark applied so we don't hammer a wedged/half-loaded AWG every tick;
            # the next edit (or Stop) re-triggers a fresh attempt.
            _applied = ver
            _status.update(connected=False, message=(
                f"{e} — lower n_points in config.yaml, or restart the galvo "
                f"container if it stays timed out"))
            time.sleep(0.5)


# ============================ Flask ============================
app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "frontend"), "index.html")


@app.route("/config")
def config():
    """Slider bounds / defaults, so the UI reflects config.yaml."""
    return jsonify({k: CFG[k] for k in
                    ("loop_hz_min", "loop_hz_max", "default_loop_hz",
                     "default_vpp", "max_vpp", "dim_travel")})


@app.route("/set_path", methods=["POST"])
def set_path():
    d = request.get_json(force=True) or {}
    with _lock:
        if "strokes" in d:
            s = d.get("strokes")
            state["strokes"] = s if s else None
        if d.get("clear"):
            state["strokes"] = None
        state["vpp"] = _clampf(d.get("vpp", state["vpp"]), 0.0, CFG["max_vpp"],
                               state["vpp"])
        state["freq"] = _clampf(d.get("freq", state["freq"]),
                                CFG["loop_hz_min"], CFG["loop_hz_max"], state["freq"])
        if "dim_travel" in d:
            state["dim_travel"] = bool(d["dim_travel"])
        if "enabled" in d:
            state["enabled"] = bool(d["enabled"])
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
    """Known-good link check: draws a slow circle via the proven sine path. If
    this works but a drawing doesn't, the fault is isolated to the arb path; if
    this fails too, the microscope isn't connected to the AWG (awg/status)."""
    try:
        _send(test_circle_cmds())
        return jsonify({"ok": True})
    except ScopioError as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/status")
def status():
    return jsonify(_status)


def main():
    global scope
    if not CFG["scopio_api_key"]:
        raise SystemExit(
            "No SCOPIO_API_KEY. Copy .env.example to .env and fill in the Pi's "
            "SCOPIO_URL and SCOPIO_API_KEY (generate a key on the Pi with "
            "`python3 ros2_ws/scripts/generate_api_key.py galvo_draw`).")
    scope = Scopio(CFG["scopio_url"], api_key=CFG["scopio_api_key"])
    # Non-fatal connection probe, so the operator sees at once if it's live; the
    # app still serves if the Pi boots later (the sender retries on use).
    try:
        scope.health()
        _status.update(connected=True, message="connected")
        print(f"  connected to microscope at {CFG['scopio_url']}")
    except ScopioError as e:
        _status.update(connected=False, message=f"not reachable yet: {e}")
        print(f"  ! microscope not reachable yet ({e}); will retry on use.")
    threading.Thread(target=sender_loop, daemon=True).start()
    print(f"galvo_draw on http://0.0.0.0:{CFG['port']}  ->  microscope {CFG['scopio_url']}")
    app.run(host="0.0.0.0", port=CFG["port"], debug=False, use_reloader=False,
            threaded=True)


if __name__ == "__main__":
    main()
