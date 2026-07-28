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

- **Publishes:** `stage/position`. **Subscribes:** `calibration`.
- **Services:** `stage/jog` (relative), `stage/move_abs`.
- **Actions:** `stage/move_path`, `scan_region`.

```bash
ros2 topic echo /scopio/stage/position
ros2 service call /scopio/stage/jog scopio_interfaces/srv/StageJog "{dx: 40, dy: 0, dz: 0}"
ros2 action send_goal /scopio/scan_region scopio_interfaces/action/ScanRegion \
  "{x_min: 0, x_max: 400, y_min: 0, y_max: 400, step: 100, settle_s: 0.3}" --feedback
```

## galvo_node — the laser AWG, exposed whole
Owns the VISA session through the `WaveGen` driver class
(`scopio_microscope/drivers/wavegen.py`, a copy of the repo-root `galvo.py`) and
offers it two ways:

- `awg/call` — **any public method of the class**, by name, with JSON args:
  sine/square/ramp, sweep, burst, AM/FM/PM/PWM/FSK, arbitrary-waveform upload,
  paced command bursts, error-queue drain.
- `awg/write` / `awg/query` — the original **raw SCPI passthrough**, unchanged,
  for clients that compose their own SCPI (`galvo_draw`, `ui/galvo_geometry.py`).

Either way the node takes no view of what commands *mean*: no volts, no pixels,
no waveform semantics. Laser *geometry* stays in client code.

- **Publishes:** `awg/status`. **Services:** `awg/call`, `awg/write`, `awg/query`.
- **Params:** `resource` (VISA address; or `GALVO_RESOURCE`), `auto_discover`,
  `timeout_ms`, `publish_rate`, `reconnect_period`.

```bash
# what can this instrument do? (name, signature, docstring for each method)
ros2 service call /scopio/awg/call scopio_interfaces/srv/InstrumentCall "{method: 'list_methods'}"
# point the X mirror to 1.25 V, two equivalent ways:
ros2 service call /scopio/awg/call scopio_interfaces/srv/InstrumentCall \
  "{method: 'set_offset', args: '[1.25, 1]'}"
ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite \
  "{command: ':SOURce1:VOLTage:OFFSet 1.2500'}"
# a continuous sine on CH1 (for a circle):
ros2 service call /scopio/awg/call scopio_interfaces/srv/InstrumentCall \
  "{method: 'apply_sine', args: '[2, 1.0]', kwargs: '{\"channel\": 1}'}"
# laser off:
ros2 service call /scopio/awg/call scopio_interfaces/srv/InstrumentCall \
  "{method: 'output', args: '[false, 1]'}"
```
> To use a *different* AWG later, swap the driver class (or just keep sending
> your own SCPI strings); the node and the ROS contract do not change.

What the class buys over the old bare-pyvisa passthrough: one lock so concurrent
clients can't interleave mid-protocol, auto-recovery (USBTMC clear, then
reconnect) after a hiccup instead of a wedged node, and `send_sequence` pacing
for the long command bursts that used to jam the session.

## temperature_node — the sample temperature controller, exposed whole
Same pattern, for a Wavelength Electronics **TC LAB** (USB/USBTMC or
Ethernet/VXI-11) through the `TCLab` driver class
(`scopio_microscope/drivers/tclab.py`, a copy of the repo-root `temperature.py`).
Every method — setpoint, PID, IntelliTune, limits, tolerance, sensor profiles,
stored profiles/scripts, raw `command`/`query` — is callable by any client.

- **Publishes:** `temperature/status` (polled at `publish_rate`).
  **Service:** `temperature/call`.
- **Params:** `resource` (or `TCLAB_RESOURCE`), `auto_discover`, `publish_rate`,
  `timeout_ms`, `reconnect_period`, `units` (forced on connect so the published
  degrees are unambiguous).

```bash
ros2 topic echo /scopio/temperature/status
ros2 service call /scopio/temperature/call scopio_interfaces/srv/InstrumentCall \
  "{method: 'set_setpoint', args: '[25.0]'}"
ros2 service call /scopio/temperature/call scopio_interfaces/srv/InstrumentCall \
  "{method: 'output', args: '[true]'}"      # nothing heats/cools until this is on
```
> **Two USB instruments, one bus:** with both the AWG and the controller on USB,
> name `GALVO_RESOURCE` *and* `TCLAB_RESOURCE` in `ros2_ws/.env` (see
> `.env.example`) — auto-discovery picks the first USB device it sees, which is
> a coin flip. An Ethernet TC LAB must always be named (`TCPIP::<ip>::INSTR`);
> pyvisa-py cannot scan the LAN.
>
> **Ramping is a client concern.** The controller has no ramp command, so an app
> that wants one walks the setpoint itself (`ui/run_ui.py` does, at °/min) —
> same node/app split as the galvo geometry.

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

## No image-analysis node — by design
There is deliberately **no tracker/detector node**. The backend senses, streams
and effectuates; it never looks at the picture. Bead detection, tracking and
every decision derived from them run on the client side, off the Pi
(`../../viscosity`, `../../viscosity_agent`), on the MJPEG stream or on
locally-recorded clips. That keeps the Pi's CPU for the camera and the stage,
and keeps trackpy/pandas/scipy out of the image entirely.

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
