# SCOPIO Nodes — what each does and how to connect

All nodes run under the `/scopio` namespace and **degrade gracefully**: if their
hardware is absent they still start and report `connected = false`, so the graph
always comes up. See `INTERFACES.md` for the frozen field-level contract.

Bring the **backend** up (drivers only — the UI is a separate app, see below):
```bash
ros2 launch scopio_microscope microscope.launch.py
```

The **web UI is a separate application** at repo-root `../ui` (a standalone ROS
client, not a workspace package). Start it independently — on the Pi or another
machine — once the backend is up: `cd ../ui && docker compose up` (or
`python3 run_ui.py`). See `../ui/README.md`.

To connect from your own program, you only need ROS 2 sourced and the same
`ROS_DOMAIN_ID` (default 0) on the same network — discovery is automatic.

---

## camera_node — owns the Pi camera (picamera2)
Pure sensor + control surface. **It does not record** (recording is a client
job — the `../ui` app saves the stream to its own folder).

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
(`microscope/galvo_geometry.py`).

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
default. (Kept in the contract; the standalone tracking *application* is a later
project — for now the monolith UI is the debugging tool for tracking.)

## The web UI — a SEPARATE app (`../ui`), not a node in this workspace
The UI lives at repo-root `../ui` as a standalone ROS client (owns no hardware).
It subscribes to the topics above, calls the services, and serves the SCOPIO
frontend so the browser UI behaves as before. Two things it does itself, **on
whatever machine runs it**:
- **Recording:** saves the subscribed stream to mp4 in its *own* local
  `ui/recordings/` folder (the Pi never records).
- **Galvo geometry:** turns UI jogs into SCPI via `GalvoClient` → `awg/write`.

```bash
cd ../ui && docker compose up          # then open http://<host>:8080
#   or, natively:  python3 run_ui.py
```
It is intentionally outside this backend workspace so it can run on a different
machine and so the backend stays a clean, headless ROS service. See
`../ui/README.md`.

### Minimal Python client (template for any external program)
```python
import rclpy
from rclpy.node import Node
from scopio_interfaces.srv import StageJog

rclpy.init()
n = rclpy.create_node("my_controller")
cli = n.create_client(StageJog, "/scopio/stage/jog")
cli.wait_for_service()
req = StageJog.Request(); req.dx, req.dy, req.dz = 40, 0, 0
fut = cli.call_async(req); rclpy.spin_until_future_complete(n, fut)
print(fut.result())
```
