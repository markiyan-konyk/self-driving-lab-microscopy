# SCOPIO Nodes — what each does and how to connect

All nodes run under the `/scopio` namespace and **degrade gracefully**: if their
hardware is absent they still start and report `connected = false`, so the graph
always comes up. See `INTERFACES.md` for the frozen field-level contract.

Bring the **backend** up (all of it — graph, camera, gateway):
```bash
cd ros2_ws && docker compose up -d
```

**External programs do not join this graph.** They go through the API gateway
(`http://<pi>:8000`, API key) — see [`../../docs/API.md`](../../docs/API.md)
and the `scopio_client` SDK. The `ros2 ...` commands below are for **backend
development on the Pi** (run them inside the `scopio` container).

---

## camera_node — the graph's camera surface (native or bridge mode)
Pure sensor + control surface. **It does not record** (recording is a client
job — the `../ui` app saves the stream to its own folder).

Two modes, picked automatically at startup:
- **native**: picamera2 importable (running natively on Pi OS) → owns the
  sensor directly.
- **bridge** (the normal case — always inside the Ubuntu container): the
  sensor is owned by `../camera_server` (its own compose service / systemd
  unit). The node ingests its MJPEG stream over loopback (`CAMERA_URL`,
  default `http://127.0.0.1:8081`), republishes the JPEGs on
  `image/compressed`, forwards the camera services to its HTTP API, and runs
  autofocus on the ingested frames. The frozen interface behaves identically
  either way. (Exception: `green_gain` is a software per-frame tweak that only
  applies in native mode.)

- **Publishes:** `image/compressed` (JPEG), `camera/state` (settings + real fps).
- **Services:** `camera/set_controls`, `camera/set_framerate`, `camera/white_balance`
  (one-shot hardware auto-white-balance).
- **Actions:** `camera/autofocus` — sweeps Z (via the `stage/jog` service),
  measures sharpness on its own frames, parks at the sharpest Z. Available to any
  client, not just the UI.
- **Params:** `width`, `height`, `framerate`, `publish_fps`, `jpeg_quality`.
- One frame-rate knob: setting fps auto-derives exposure + analogue gain to hold
  brightness (the "exposure budget"), so high fps no longer goes dark.

```bash
ros2 topic echo /scopio/camera/state
ros2 service call /scopio/camera/set_framerate scopio_interfaces/srv/SetFramerate "{fps: 60.0}"
ros2 service call /scopio/camera/white_balance scopio_interfaces/srv/WhiteBalance "{}"
# leave a field unchanged by passing NaN (.nan in YAML):
ros2 service call /scopio/camera/set_controls scopio_interfaces/srv/SetCameraControls \
  "{red_gain: 2.4, green_gain: .nan, blue_gain: 2.5, colour_gain: .nan, analogue_gain: .nan, \
    contrast: .nan, saturation: .nan, brightness: .nan, sharpness: .nan}"
```

## stage_node — owns the Sangaboard XYZ stage
Tracks open-loop absolute position (accumulated from relative moves) and reports
it in steps and micrometres.

- **Publishes:** `stage/position`. **Subscribes:** `beads`, `calibration`.
- **Services:** `stage/jog` (relative), `stage/move_abs`.
- **Actions:** `stage/move_path`, `scan_region`.

```bash
ros2 topic echo /scopio/stage/position
ros2 service call /scopio/stage/jog scopio_interfaces/srv/StageJog "{dx: 40, dy: 0, dz: 0}"
ros2 action send_goal /scopio/scan_region scopio_interfaces/action/ScanRegion \
  "{x_min: 0, x_max: 400, y_min: 0, y_max: 400, step: 100, settle_s: 0.3}" --feedback
```

## galvo_node — raw VISA/SCPI passthrough to the laser AWG
**Instrument-agnostic and frozen.** It relays command strings to the AWG and
relays query replies back. It knows nothing about volts/pixels/waveforms — the
caller must speak the instrument's SCPI. Laser *geometry* lives in client code
(`../ui/galvo_geometry.py`).

- **Publishes:** `awg/status`. **Services:** `awg/write`, `awg/query`.
- **Param:** `resource` (VISA address; or the `GALVO_RESOURCE` env var).

```bash
ros2 service call /scopio/awg/query scopio_interfaces/srv/AwgQuery "{command: '*IDN?'}"
# point the X mirror to 1.25 V:
ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite "{command: ':SOURce1:VOLTage:OFFSet 1.2500'}"
# a continuous sine on CH1 (for a circle): 
ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite "{command: ':SOURce1:APPLy:SINusoid 2,1.0,0'}"
# laser off:
ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite "{command: ':OUTPut1 OFF'}"
```
> To use a *different* AWG later, change only your SCPI strings (or write a
> sibling of `GalvoClient`); the ROS node does not change.

## calibration_node — owns the spatial calibration
Single source of truth for µm/px (image scale) and steps/µm (stage). Persisted
to disk; published **latched** so any client gets it on join.

- **Publishes:** `calibration` (latched). **Service:** `calibration/set`.
- **Param:** `calibration_file` (JSON path; persists across restarts).

```bash
ros2 topic echo --once /scopio/calibration
ros2 service call /scopio/calibration/set scopio_interfaces/srv/CalibrationSet \
  "{um_per_px: 0.42, steps_per_um_x: .nan, steps_per_um_y: .nan, steps_per_um_z: .nan}"
```

## tracker_node — bead detection (on-demand)
Subscribes to `image/compressed`, runs trackpy, publishes `beads`. Off by
default. (Kept in the contract; the standalone tracking *application* is a
later project. trackpy/pandas/scipy are still not installed in the image — see
the Dockerfile note — so this node currently idles even when toggled on.)

## gateway — the API gateway (scopio_gateway, in this workspace)
FastAPI + rclpy node that maps the whole graph to authenticated HTTP/WebSocket
on port 8000: generic `POST /api/v1/service/{name}`, WS topic subscriptions +
actions, an MJPEG proxy of the camera server, and `GET /api/v1/interfaces`
discovery. New nodes appear in the API automatically — the mapping is
introspected from the live graph, not hand-coded. See
[`../../docs/API.md`](../../docs/API.md).

## The client apps — SEPARATE programs, not nodes
`../ui` (web UI, records locally, galvo geometry via `GalvoClient`) and
`../galvo_draw` (laser vector drawing) are plain API clients built on the
`scopio_client` SDK. They run on any machine that can reach the gateway.

### Minimal Python client (template for any external program)
```python
from scopio_client import Scopio

scope = Scopio("http://<pi-ip>:8000", api_key="<key>")
print(scope.stage.jog(dx=40))                       # {'success': True, ...}
scope.subscribe("stage/position", lambda msg, env: print(msg), rate_hz=5)
result = scope.camera.autofocus(on_feedback=print)  # backend action
```
(No ROS required. If you're writing a *backend* node instead, see the existing
nodes in `scopio_microscope/` as templates.)
