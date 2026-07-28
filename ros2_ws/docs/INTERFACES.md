# SCOPIO ROS 2 Interface Control Document — `scopio_interfaces` v2.0

**This is the contract. Treat it as frozen.** Any program — a UI, an offline
bead-tracking pipeline, a galvo art app, an autonomous controller — drives the
microscope by speaking exactly these topics, services, and actions. Changing a
field is an ABI break: bump the version and update this document deliberately;
do not edit in place.

**Scope of the backend:** it senses, streams and effectuates. It does **no**
image analysis, so nothing in this contract carries detections or derived
scene content — clients compute that themselves from the video.

- **Namespace:** everything is under `/scopio` (e.g. `/scopio/image/compressed`).
- **Units:** stage = Sangaboard *steps* (and *micrometres* where noted); image =
  native camera *pixels*; laser/AWG = whatever SCPI the instrument speaks (the
  ROS layer is unit-agnostic for the AWG — see `awg/*`).
- **Frames:** image coords are native sensor pixels, origin top-left. Stage
  "global" position is open-loop, origin = wherever the stage was at node start.
- **QoS:** sensor topics use depth-5 KEEP_LAST (best-effort default). `calibration`
  is **latched** (TRANSIENT_LOCAL, depth 1) so late joiners get it immediately.

---

## Topics (sensors / state — published by the drivers)

| Topic | Type | Publisher | Notes |
|-------|------|-----------|-------|
| `image/compressed` | `sensor_msgs/CompressedImage` | camera_node | JPEG live view. Publish rate is a node param (≤ capture fps). |
| `camera/state` | `scopio_interfaces/CameraState` | camera_node | Current settings + **real measured fps**. |
| `stage/position` | `scopio_interfaces/StagePosition` | stage_node | Open-loop position in **steps and micrometres**. |
| `awg/status` | `scopio_interfaces/AwgStatus` | galvo_node | AWG liveness + last relayed command. |
| `temperature/status` | `scopio_interfaces/TemperatureStatus` | temperature_node | Sample temperature, setpoint, TEC current/voltage, fault flags. |
| `calibration` | `scopio_interfaces/Calibration` | calibration_node | **Latched.** µm/px + steps/µm. |

### v1.1 — additive only
`TemperatureStatus` and `InstrumentCall` were **added**; no existing field
changed, so every v1.0 client keeps working untouched.

### v2.0 — breaking: on-Pi image analysis removed
`tracker_node` and everything it fed are **gone**: the `beads` topic, the
`tracker/set_active` service, `msg/Bead`, `msg/BeadArray`, and the
`beads_in_frame` field of `ScanRegion` feedback. Running bead detection on the
Pi was a design error — it spends the microscope's CPU on work a client can do
better on the stream or on recorded clips, and it dragged trackpy/pandas/scipy
into the backend image. The Pi now senses and effectuates only; detection lives
in `../../viscosity` and `../../viscosity_agent`. Clients that subscribed to
`beads` must drop that subscription (the gateway answers `unknown_topic`).

> The earlier plan was `sensors/temperature` + `sensor_msgs/Temperature`. It was
> dropped: a bare temperature reading can't carry the setpoint, TEC current or
> the sensor-fault flags a control loop needs, and the controller is an
> *effectuator* as much as a sensor. One purpose-built status message beats a
> standard one that omits half the state.

## Services (effectuators / commands)

| Service | Type | Server | Purpose |
|---------|------|--------|---------|
| `stage/jog` | `StageJog` | stage_node | Relative jog by `dx,dy,dz` steps (low-latency; UI hold). |
| `stage/move_abs` | `MoveAbs` | stage_node | Single absolute move (steps). |
| `camera/set_controls` | `SetCameraControls` | camera_node | Set any subset of gains/colour/contrast/etc. (NaN = leave). |
| `camera/set_framerate` | `SetFramerate` | camera_node | Set fps; node derives exposure + gain (budget math). |
| `camera/white_balance` | `WhiteBalance` | camera_node | One-shot hardware AWB; returns the gains. |
| `awg/write` | `AwgWrite` | galvo_node | **Relay a raw SCPI command** to the AWG. |
| `awg/query` | `AwgQuery` | galvo_node | **Relay a raw SCPI query**, return the reply. |
| `awg/call` | `InstrumentCall` | galvo_node | **Call any method** of the AWG driver class (`drivers/wavegen.py`). |
| `temperature/call` | `InstrumentCall` | temperature_node | **Call any method** of the controller driver class (`drivers/tclab.py`). |
| `calibration/set` | `CalibrationSet` | calibration_node | Update µm/px or steps/µm (NaN = leave), persisted. |

### The instrument-call pattern (`InstrumentCall`)

Two nodes own a plain-python driver **class** and expose *all* of it through one
service: `{method, args, kwargs}` in (args/kwargs are JSON strings), `{success,
result, error}` out (`result` is JSON). It is the one place this contract is
deliberately not statically typed, because the alternative — a service per
instrument feature — would mean an ABI change every time an experiment needs a
knob the last one didn't.

```bash
ros2 service call /scopio/temperature/call scopio_interfaces/srv/InstrumentCall \
  "{method: 'set_setpoint', args: '[25.0]'}"
ros2 service call /scopio/temperature/call scopio_interfaces/srv/InstrumentCall \
  "{method: 'list_methods'}"          # self-describing: name, signature, doc
```

Rules: private methods (`_x`) and `close` are not reachable; unknown methods and
bad arguments come back as `success: false` **without** disturbing the
instrument; `list_methods`, `connected` and `reconnect` are answered by the node
itself and work even while the hardware is offline.

## Actions (long-running, with feedback)

| Action | Type | Server | Purpose |
|--------|------|--------|---------|
| `stage/move_path` | `MoveStagePath` | stage_node | Visit a list of absolute targets, report progress. |
| `scan_region` | `ScanRegion` | stage_node | Boustrophedon grid scan, report position per stop (the client reads the frames). |
| `camera/autofocus` | `Autofocus` | camera_node | Sweep Z (via `stage/jog`), measure sharpness on the node's own frames, park at the sharpest Z. |

> **Autofocus is a backend action** (`camera/autofocus`), so every client — the
> UI *and* any external program — gets the same one-call autofocus, instead of
> each reimplementing the Z-sweep. The camera node hosts it because it has the
> frames; it drives Z through the stage's `stage/jog` service. (Auto white
> balance is similarly first-class: the `camera/white_balance` service runs the
> hardware AWB and returns the gains it locked in.)

---

## Message / service / action fields

### msg/CameraState
`header`, `bool connected`, `float32 target_fps`, `float32 measured_fps`,
`int32 exposure_us`, `float32 analogue_gain`, `float32 red_gain`,
`float32 green_gain`, `float32 blue_gain`, `float32 colour_gain`,
`float32 contrast`, `float32 saturation`, `float32 brightness`,
`float32 sharpness`, `int32 width`, `int32 height`.

### msg/StagePosition
`header`, `bool connected`, `int32 x/y/z` (steps), `float32 x_um/y_um/z_um`
(micrometres via calibration; 0 if uncalibrated).

### msg/AwgStatus
`header`, `bool connected`, `string idn`, `string last_command`,
`string last_error`.

### msg/Calibration  (latched)
`header`, `bool has_um_per_px`, `float64 um_per_px`,
`float64 steps_per_um_x/y/z`.

### msg/StagePoint
`int32 x`, `int32 y`, `int32 z` — one absolute stage target, in steps.

### srv/StageJog → `int32 dx,dy,dz` ⇒ `bool success, string message, int32 x,y,z`
### srv/MoveAbs → `int32 x,y,z` ⇒ `bool success, string message, int32 x,y,z`
### srv/SetCameraControls → 9× `float64` (NaN = leave) ⇒ `bool success, string message`
### srv/SetFramerate → `float64 fps` ⇒ `bool success, float64 framerate, int32 exposure_us, float64 analogue_gain`
### srv/WhiteBalance → (empty) ⇒ `bool success, float64 red_gain, blue_gain, string message`
### srv/AwgWrite → `string command` ⇒ `bool success, string error`
### srv/AwgQuery → `string command` ⇒ `bool success, string response, string error`
### srv/CalibrationSet → `float64 um_per_px, steps_per_um_x/y/z` (NaN = leave) ⇒ `bool success, string message`

---

## Versioning / compatibility policy
- v1.0 is the first frozen baseline. Additive, backward-compatible changes
  (new optional topics/services, new trailing message fields consumed only by
  new clients) may ship as **v1.x**.
- Removing/renaming/retyping any field, topic, service, or action is a
  **breaking change → v2.0**, announced here, with the old version kept running
  during migration where possible.
- Retired in the move to v1.0 (do not resurrect under the same names): the
  node-side recording (`recording/set`, `recording/status`) and the typed galvo
  API (`SetLaser`, `ZeroTweezers`, `RunGalvoWaveform`, geometry in `LaserState`).
- Retired in the move to v2.0 (do not resurrect): on-Pi image analysis —
  `beads`, `tracker/set_active`, `Bead`, `BeadArray`, and
  `ScanRegion.Feedback.beads_in_frame`. Recording and analysis are both client
  concerns; the backend stays a sensor + effectuator.
