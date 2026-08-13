# SCOPIO Web UI (standalone API client)

This is the SCOPIO microscope's web interface. It is a **separate application
from the backend** (`../ros2_ws`) on purpose: it owns no hardware and talks to
the microscope purely through the **API gateway** (HTTP/WebSocket + API key)
via the `scopio_client` SDK. It runs on **any machine that can reach the Pi** —
no ROS, no Docker, no DDS, no special network setup. Several people can each
run their own UI against the same microscope.

```
backend (Pi: ros2_ws + gateway)  ──HTTP/WS──►  this UI  ──HTTP/MJPEG──►  your browser
      drivers own hardware                     API client, records locally
```

- **Video** is the gateway's `/api/v1/stream.mjpg` ingested by the SDK.
- **Telemetry** (stage position, camera state, AWG status, temperature, relay
  state, calibration) streams over the SDK's WebSocket subscriptions.
- **Controls** are SDK calls (`scope.stage.jog`, `scope.calibration.set`,
  `scope.galvo.call`, `scope.temperature.setpoint`, `scope.laser.set`...).
- **Autofocus** runs on the backend (`camera/autofocus` action).
- **Detail / Fast** switches the sensor between its full-field high-resolution
  mode and its high-frame-rate mode. The readout under the toggle gives the real
  size and rate, which is why there is no jittery measured-fps number any more.
- **Freeze** holds the picture so you can measure on it; the stream keeps
  arriving underneath and recording is unaffected. **Screenshot** saves what you
  are looking at — held frame, scale bar and measurement burnt in — as a JPEG in
  the clip folder.
- **Measuring**: *Set Scale* on a known distance, then *Measure*. The scale is
  stored with the frame width it was measured at, so switching sensor mode
  rescales it instead of invalidating it — the readout always states the
  resolution the number applies to.
- **Recording happens here**, on the machine running this program — pick the
  folder in the left column (**Save Clips To**). The Pi takes no recording load,
  and since the browser may be on a different machine entirely, that panel names
  the host the files actually land on.
- **Laser** is one switch at the top of the left column: the `relay/set` service
  on a Pi GPIO pin. It is the only amber control on the page, on purpose. When
  the relay is unreachable the switch shows the *last known* state and says so —
  an unreachable laser is unknown, never off.
- **Galvo** is two sliders (X = CH1, Y = CH2) that only *stage* a voltage; one
  `update()` per channel is sent when **Update Galvo** is pressed, because the
  wavegen's command buffer is small and slider-drag streaming overruns it.
- **Temperature** is one row: live reading, a setpoint box, and
  an Enable button. Those are two separate commands, mirroring the instrument —
  typing a number only stores the target, and the TEC drives nothing until
  Enable is on. The reading's colour is the whole status display: grey = idle,
  amber = driving, green = at setpoint, red = offline or faulted (hover for the
  fault). The backend node exposes the controller's *entire* driver class over
  `temperature/call` — PID, IntelliTune, limits, sensor calibration, ramps — of
  which this panel deliberately uses only reading / setpoint / TEC-output. A
  dedicated temperature app can use the rest; `scope.temperature.methods()`
  lists all ~110. (No ramp here: the controller has no ramp command, so walking
  the setpoint over time is a client policy nobody has needed yet.)

## Prerequisite: the backend must be running

On the Pi: `cd ros2_ws && docker compose up -d` — and generate yourself a key:
`python3 ros2_ws/scripts/generate_api_key.py ui`.

## Run

```bash
pip install -r requirements.txt        # includes the scopio_client SDK

# Windows (PowerShell)                 # Linux/macOS
$env:SCOPIO_URL="http://<pi-ip>:8000"  export SCOPIO_URL=http://<pi-ip>:8000
$env:SCOPIO_API_KEY="<key>"            export SCOPIO_API_KEY=<key>

python run_ui.py                       # -> http://localhost:8080
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `SCOPIO_URL` | `http://127.0.0.1:8000` | the microscope's API gateway |
| `SCOPIO_API_KEY` | *(required)* | key from `generate_api_key.py` |
| `SCOPIO_UI_PORT` | `8080` | HTTP port of this UI |
| `SCOPIO_UI_PASSWORD` | `password` | browser login password |
| `SCOPIO_RECORDINGS_DIR` | `./recordings` | initial clip folder (changeable in the UI) |

## Test

```bash
python ui/test_run_ui.py
```

No microscope, no gateway. It covers the recorder, which is the part of this app
that fails quietly rather than loudly: one written frame per ingested frame (the
clip's frame count is its timebase, and any motion analysis done later is only
as good as that), and the interlock that stops a second recording from opening
over a file the first is still closing.

## Notes / current limitations

- The galvo voltages shown are the node's own bookkeeping, so an agent or a
  second UI moving the mirrors does show up here. The AWG itself has no position
  readback, so a hand on its front panel is the one thing nothing can see.
- No per-control camera panel. Exposure, gains and white balance are set once
  per sample and then left alone, and a wall of sliders crowded out the controls
  an operator actually touches; the sensor MODE is the one camera choice worth a
  button, and it has one. Anything else goes to the gateway directly
  (`scope.camera.set_controls(...)`, or the `camera_controls` MCP tool).
- If several UIs jog the stage at the same time they interleave safely at the
  hardware level, but can obviously fight each other logically — coordinate
  with your labmates.
