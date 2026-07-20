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
- **Telemetry** (stage position, camera state, AWG status, calibration) streams
  over the SDK's WebSocket subscriptions.
- **Controls** are SDK calls (`scope.stage.jog`, `scope.camera.set_controls`,
  `scope.calibration.set`, `scope.galvo.write`...).
- **Autofocus** runs on the backend (`camera/autofocus` action).
- **Recording happens here**, written to `./recordings` on whatever machine
  runs the UI (the Pi takes no recording load).
- **Galvo/laser** control composes SCPI strings in `galvo_geometry.py` and
  sends them through the `awg/write` passthrough.

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

## Notes / current limitations

- The laser position shown is what *this* UI commanded (the AWG has no readback
  in the status topic) — same single-owner assumption as before.
- `galvo_geometry.py` here is the client-side geometry helper (volts↔pixels↔µm,
  homing, jogging); any external galvo app can reuse the same module.
- If several UIs jog the stage at the same time they interleave safely at the
  hardware level, but can obviously fight each other logically — coordinate
  with your labmates.
