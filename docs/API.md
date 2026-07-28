# SCOPIO Microscope API — developer manual

Everything the microscope can do, over plain HTTP/WebSocket with an API key.
This is the contract external programs (the UI, galvo_draw, agents, your own
scripts) build against. **No ROS, no Docker, no DDS is needed on the client.**

```
 your program ──HTTP/WS + API key──► gateway (Pi :8000) ──ROS 2 (localhost)──► driver nodes
                                        │
                                        └──loopback HTTP──► camera server (:8081)
```

The gateway translates JSON into calls on the frozen ROS 2 interface contract
(`ros2_ws/src/scopio_interfaces/`). The three ROS interaction patterns map to
three transports:

| ROS concept | What it's for | How you use it |
|---|---|---|
| **service** | request/response commands (jog, set controls, SCPI...) | `POST /api/v1/service/{name}` |
| **topic** | continuous telemetry streams (position, status...) | WebSocket `subscribe` |
| **action** | long-running jobs with progress (autofocus, scans) | WebSocket `action_send_goal` |

Video is special: it streams as MJPEG at `GET /api/v1/stream.mjpg`, never as
JSON.

Interactive route docs are served live at `http://<pi>:8000/docs`, and
`GET /api/v1/interfaces` returns the machine-readable list of every service/
topic/action with field schemas — **new capabilities (e.g. the planned
temperature node) appear there automatically with zero gateway changes.**

---

## 1. Authentication

Keys are generated **on the Pi** and stored in `ros2_ws/secrets/api_keys.json`
(gitignored; the gateway hot-reloads it):

```bash
python3 ros2_ws/scripts/generate_api_key.py ui        # create/rotate a key
python3 ros2_ws/scripts/generate_api_key.py --list
python3 ros2_ws/scripts/generate_api_key.py --revoke ui
```

Send the key with every request, either way:

- header (preferred): `X-API-Key: <key>`
- query param (for browser `<img>` tags and WebSockets): `?api_key=<key>`

Missing/invalid key → `401`. Only `GET /api/v1/health` is unauthenticated.

**Security posture:** the key travels in plain HTTP, which is fine on a
trusted lab LAN. If you ever expose the microscope beyond the lab, put it
behind TLS (a reverse proxy) or a VPN like Tailscale — don't port-forward it
raw. The camera server on :8081 binds to loopback only; the gateway is the
single external door.

## 2. Quick start

```bash
curl http://<pi>:8000/api/v1/health
curl -H "X-API-Key: $KEY" http://<pi>:8000/api/v1/status
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"dx": 0, "dy": 0, "dz": 100}' \
     http://<pi>:8000/api/v1/service/stage/jog
```

Or with the SDK (`pip install -e ./scopio_client`):

```python
from scopio_client import Scopio
scope = Scopio("http://<pi>:8000", api_key=KEY)
scope.stage.jog(dz=100)
```

## 3. HTTP endpoints

| Method + path | Auth | Meaning |
|---|---|---|
| `GET /api/v1/health` | no | `{ok, ros_ok, camera_ok, auth_configured, uptime_s}` |
| `GET /api/v1/status` | yes | one-call snapshot: latest `stage/position`, `camera/state`, `awg/status`, `temperature/status`, `calibration` + camera reachability |
| `GET /api/v1/interfaces` | yes | discovery: every service/topic/action with per-field schemas |
| `POST /api/v1/service/{name}` | yes | **generic service call** (section 4) |
| `GET /api/v1/stream.mjpg` | yes | live MJPEG video |
| `GET /api/v1/camera/controls` | yes | current camera settings (live exposure/gain merged in) |
| `POST /api/v1/camera/controls` | yes | partial update: `framerate, exposure, analogue_gain, red_gain, blue_gain, contrast, saturation, brightness, sharpness` |
| `POST /api/v1/camera/white_balance` | yes | one-shot AWB; locks the measured gains |
| `GET /api/v1/camera/focus` | yes | cheap focus metric |
| `WS /api/v1/ws` | yes | topics + actions (section 5) |

## 4. Generic service calls

`POST /api/v1/service/{name}` where `{name}` is relative to `/scopio`
(e.g. `stage/jog`; an absolute path like `/other/thing` also works). The JSON
body maps 1:1 to the service's request fields; the response is the service's
response as JSON. Optional `?timeout=<seconds>` (default 10).

Errors: `404` unknown service · `422` bad fields · `504` node/hardware timeout.

### The NaN rule (read this once, it will save you an afternoon)

`camera/set_controls` and `calibration/set` use **NaN = "leave this field
unchanged"**. JSON has no NaN, and a float field you *omit* is built as `0.0`
— which would actively clobber the setting. So:

- **JSON `null`** on a float field → NaN → *unchanged*
- **omitted** float field → `0.0` (the ROS default!)

```json
// SETS contrast, ZEROES every other control (probably not what you want):
{"contrast": 1.2}
// SETS contrast, leaves the rest alone:
{"contrast": 1.2, "red_gain": null, "green_gain": null, "blue_gain": null,
 "colour_gain": null, "analogue_gain": null, "saturation": null,
 "brightness": null, "sharpness": null}
```

The SDK convenience methods (`scope.calibration.set(...)`) pre-fill the nulls
for you.

### Service reference

All types live in `ros2_ws/src/scopio_interfaces/srv/` — the definitions there
(with comments) are authoritative. Summary:

#### `stage/jog` — relative stage move (StageJog)
Request `{dx, dy, dz}` (int, Sangaboard steps) →
`{success, message, x, y, z}` (resulting absolute steps).
```python
scope.stage.jog(dz=100)
```

#### `stage/move_abs` — absolute stage move (MoveAbs)
Request `{x, y, z}` → `{success, message, x, y, z}`.

#### `camera/set_controls` — manual camera controls (SetCameraControls, NaN rule!)
Request `{red_gain, green_gain, blue_gain, colour_gain, analogue_gain,
contrast, saturation, brightness, sharpness}` (float or null) →
`{success, message}`. `analogue_gain` is treated as a brightness ("exposure
budget") change that survives frame-rate changes.
*Tip: the curated `POST /api/v1/camera/controls` endpoint has friendlier
partial-update semantics (omit = unchanged) and also accepts `framerate` and
`exposure`.*

#### `camera/set_framerate` — fps with exposure budget (SetFramerate)
Request `{fps}` → `{success, framerate, exposure_us, analogue_gain}` (what was
actually applied after clamping/derivation).

#### `camera/white_balance` — one-shot hardware AWB (WhiteBalance)
Request `{}` → `{success, red_gain, blue_gain, message}`. Takes ~1.5 s.

#### `calibration/set` — persist spatial calibration (CalibrationSet, NaN rule!)
Request `{um_per_px, steps_per_um_x, steps_per_um_y, steps_per_um_z}` (float
or null) → `{success, message}`. Persisted to disk and re-published on the
latched `calibration` topic.
```python
scope.calibration.set(um_per_px=0.42)      # nulls pre-filled for the rest
```

#### `awg/write` — raw SCPI command to the galvo/laser AWG (AwgWrite)
Request `{command}` (string, e.g. `":SOURce1:VOLTage:OFFSet 1.2500"`) →
`{success, error}`. The caller owns the instrument's command language — the
backend is a deliberate dumb passthrough (see `DECISIONS.md`).

#### `awg/query` — raw SCPI query (AwgQuery)
Request `{command}` (e.g. `"*IDN?"`) → `{success, response, error}`.

#### `awg/call` / `temperature/call` — call any driver method (InstrumentCall)
See section 4.1 — this is how you reach everything the AWG and the temperature
controller can do.

### 4.1 Instrument calls: the whole driver class, one service

The galvo and temperature nodes each own a plain-python driver **class** and
expose *every public method of it* through a single service. There is no fixed
list of "supported features": whatever the class can do, your app can do, and a
method added to the driver is callable the same day — no new endpoint, no
gateway change, no SDK update.

| Service | Instrument | Driver class |
|---|---|---|
| `awg/call` | Rigol DG1022Z AWG (galvo mirrors) | `ros2_ws/…/drivers/dg1022z.py` (`DG1022Z`) |
| `temperature/call` | Wavelength TC10 LAB controller | `ros2_ws/…/drivers/TC10LAB.py` (`TC10LAB`) |

Request `{method, args, kwargs}` → `{success, result, error}`. `args` is a JSON
**array**, `kwargs` a JSON **object**, both as *strings* (ROS fields are
statically typed; this is the escape hatch). `result` is the JSON-encoded return
value.

```bash
# set the sample to 25 °C
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"method": "set_setpoint", "args": "[25.0]"}' \
     http://<pi>:8000/api/v1/service/temperature/call
# enable the TEC output -- nothing heats or cools until this is on
     -d '{"method": "output", "args": "[true]"}'
# a 1 kHz sine on CH2 of the AWG
     -d '{"method": "apply_sine", "args": "[1000, 2.0]", "kwargs": "{\"channel\": 2}"}'
```

**Ask the instrument what it can do** — `list_methods` returns the name,
signature and docstring of everything callable, so you never have to guess:

```python
for m in scope.temperature.methods():
    print(m["name"], m["signature"], "--", m["doc"])
# set_setpoint (value) -- ...
# set_pid (p, i=None, d=None) -- ...
# intellitune_start () -- ...
```

SDK:
```python
scope.temperature.setpoint(25.0)          # sugar for the common calls
scope.temperature.output(True)
scope.temperature.call("set_pid", 1.2, i=0.4)   # anything else
scope.galvo.call("apply_sine", 1000, 2.0, channel=2)
```

Three **meta-methods** are answered by the node itself and work even while the
hardware is offline: `list_methods`, `connected`, `reconnect`.

Rules of the road:
- Private methods (`_foo`) and `close` are not reachable.
- An unknown method or bad argument returns `success: false` and changes
  nothing — one client's mistake can't knock the instrument offline for others.
- A genuine link failure drops the session; the node reconnects on its own
  (~15 s) and `connected` goes false on the status topic meanwhile.
- **Policy stays in your app.** The controller has no ramp command, so ramping
  is done by walking the setpoint client-side (`ui/run_ui.py` does this at
  °/min) — exactly like galvo geometry lives in client code.

## 5. WebSocket: topics + actions

Connect to `ws://<pi>:8000/api/v1/ws?api_key=<key>`. Every frame both ways is
a JSON object with an `"op"`; you pick an `"id"` per request and all frames
about that request echo it.

### Subscribing to topics

```json
→ {"op": "subscribe", "id": "s1", "topic": "stage/position", "rate_hz": 10}
← {"op": "ok", "id": "s1"}
← {"op": "message", "id": "s1", "topic": "stage/position",
   "stamp": 1789000000.123, "msg": {"x": 120, "y": 0, "z": 4400, ...}}
→ {"op": "unsubscribe", "id": "s1"}
```

- `rate_hz` (optional) decimates server-side; without it you get every message.
- Latched topics (`calibration`) deliver their retained value immediately.
- Video topics are refused with `{"op":"error","code":"use_mjpeg"}` — use
  `GET /api/v1/stream.mjpg`.

**Topics** (types in `ros2_ws/src/scopio_interfaces/msg/`):

| topic | message | content |
|---|---|---|
| `stage/position` | StagePosition | absolute steps + µm, `connected` |
| `camera/state` | CameraState | all camera settings + measured fps |
| `awg/status` | AwgStatus | AWG `connected`, resource string |
| `temperature/status` | TemperatureStatus | temperature, setpoint, TEC current/voltage, `output_enabled`, `in_tolerance`, `sensor_fault` |
| `calibration` | Calibration | µm/px + steps/µm (latched) |
| `image/compressed` | CompressedImage | *refused over WS — use the MJPEG stream* |

### Publishing to a topic

```json
→ {"op": "publish", "id": "p1", "topic": "some/topic",
   "type": "scopio_interfaces/msg/Whatever", "msg": {...}}
```
(`type` optional if the topic already has a live publisher. Rarely needed —
commands should go through services.)

### Actions (long-running jobs)

```json
→ {"op": "action_send_goal", "id": "g1", "action": "camera/autofocus",
   "goal": {"z_range": 2000, "steps": 15, "settle_s": 0.2}}
← {"op": "action_ack", "id": "g1", "accepted": true}
← {"op": "action_feedback", "id": "g1", "feedback": {"index": 0, "z": 2400, "score": 812.5}}
← ... more feedback ...
← {"op": "action_result", "id": "g1", "status": "succeeded",
   "result": {"success": true, "best_z": 4210, "best_score": 1544.2, "message": "ok"}}
→ {"op": "action_cancel", "id": "g1"}        // optional, any time before result
```

`status` is `succeeded | aborted | canceled`. Closing the socket does NOT
cancel a running goal (ROS semantics) — cancel explicitly if you need to.

**Actions** (types in `ros2_ws/src/scopio_interfaces/action/`):

| action | goal | feedback | result |
|---|---|---|---|
| `camera/autofocus` | `{z_range, steps, settle_s}` | `{index, z, score}` | `{success, best_z, best_score, message}` |
| `stage/move_path` | `{points: [{x,y,z},...], settle_s}` | `{current_index, x, y, z}` | `{success, points_reached}` |
| `scan_region` | `{x_min, x_max, y_min, y_max, step, settle_s}` | `{frames_visited, x, y}` | `{success, frames_visited}` |

SDK equivalent:
```python
result = scope.send_goal("scan_region",
                         {"x_min": 0, "x_max": 4000, "y_min": 0, "y_max": 4000,
                          "step": 500, "settle_s": 0.3},
                         on_feedback=print)
```

### WS error codes

`unauthorized` · `bad_request` · `unknown_topic` · `unknown_action` ·
`use_mjpeg` · `bad_fields` · `timeout` · `disconnected` (SDK-side, connection
lost).

## 6. Video

`GET /api/v1/stream.mjpg` is a standard `multipart/x-mixed-replace` MJPEG
stream (a proxy of the Pi camera server). Use it:

- in a browser: `<img src="http://<pi>:8000/api/v1/stream.mjpg?api_key=KEY">`
- in Python: `for jpeg in scope.stream_frames(): ...` (raw JPEG bytes;
  `cv2.imdecode` them for pixels)

## 7. Multi-client etiquette

The gateway is stateless and happily serves many clients at once (that's the
point). The driver nodes serialize hardware access, so concurrent commands
interleave *safely* — but the **stage and the AWG are single physical
resources**: two programs jogging at once will fight logically, and the laser
position has no readback (the last writer's state is the truth). For now,
coordinate usage between humans/agents; an advisory ownership/locking scheme
is possible future work in the gateway.

## 8. Adding new capabilities (for backend developers)

1. Add your node + interfaces in `ros2_ws/src/` as usual (or reuse standard
   types), launch it under `/scopio`.
2. That's it for the API — the generic endpoints and `/api/v1/interfaces`
   pick it up from the live graph automatically.
3. Document the new commands here, and (optionally) add a convenience
   namespace to `scopio_client`.

Worked example: the temperature node (section 4.1) was added exactly this way —
a node, one message and one service in `scopio_interfaces`, and it was reachable
through `/api/v1/service/temperature/call`, `/api/v1/interfaces` and the
WebSocket the moment it launched. The only gateway edit was optional: adding
`temperature/status` to the cached snapshot in `GET /api/v1/status`.

**Instrument nodes: prefer the class pattern.** If your node fronts an
instrument with a big command set, give it a driver class and one
`InstrumentCall` service instead of a service per feature. You get the whole
instrument, self-documented (`list_methods`), and the contract stops churning.
