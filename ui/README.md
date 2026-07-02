# SCOPIO Web UI (standalone ROS 2 client)

This is the SCOPIO microscope's web interface. It is a **separate application
from the ROS backend** (`../ros2_ws`) on purpose: it owns no hardware and talks
to the backend purely over ROS, so it can run **on the Pi or on any other
machine** on the same ROS graph. That separation is the whole point — the UI is
just one client of the "sacred" backend, exactly like any program you write.

```
backend (ros2_ws)  ──DDS──►  this UI  ──HTTP/MJPEG──►  your browser
   drivers own hardware       ROS client, records locally
```

- **Video** is the UI subscribing to the `image/compressed` topic.
- **Controls** call the backend's services (`stage/jog`, `camera/set_controls`,
  `camera/set_framerate`, `camera/white_balance`, `calibration/set`, `awg/write`).
- **Recording happens here**, written to `./recordings` on whatever machine runs
  the UI (the Pi takes no recording load).
- **Galvo/laser** control composes SCPI strings in `galvo_geometry.py` and sends
  them through the `awg/write` passthrough.

## Prerequisite: the backend must be running
Start it first (see `../ros2_ws`), e.g. on the Pi:
```bash
cd ../ros2_ws && docker compose up --build
```

## Run the UI — Docker (easiest)
```bash
cd ui
docker compose up --build           # -> http://localhost:8080
```

## Run the UI — natively (good for development)
Needs ROS 2 Jazzy + the built `scopio_interfaces` on your path.
```bash
# 1. Build/​source the interface package (once):
cd ../ros2_ws && colcon build --packages-select scopio_interfaces && source install/setup.bash
# 2. Python deps for the UI:
pip install -r ../ui/requirements.txt
# 3. Run it:
cd ../ui && python3 run_ui.py        # -> http://localhost:8080
```

## Running the UI on a DIFFERENT machine than the Pi
This is the headline capability. On the other machine:
1. Same LAN as the Pi, same `ROS_DOMAIN_ID` (default `0`).
2. Have ROS 2 + `scopio_interfaces` (build it from this repo there, or use the
   Docker image which builds it for you).
3. `docker compose up --build` (or native run). The UI discovers the Pi's nodes
   over DDS automatically.
4. Confirm discovery first if unsure: `ros2 topic list` should show `/scopio/...`.
   If it doesn't, it's almost always WiFi blocking multicast — use wired
   ethernet or set Fast-DDS unicast peers (see `../ros2_ws/docs/CONNECTIVITY.md`).

## Environment variables
| Var | Default | Meaning |
|-----|---------|---------|
| `SCOPIO_UI_PORT` | `8080` | HTTP port |
| `SCOPIO_UI_PASSWORD` | `password` | login password |
| `SCOPIO_NAMESPACE` | `/scopio` | backend ROS namespace |
| `ROS_DOMAIN_ID` | `0` | must match the backend |

## Notes / current limitations
- **Autofocus** is not wired into this UI yet (it is a composed routine, not a
  driver primitive). The monolith (`../microscope`) still has it for debugging.
- The laser position shown is what *this* UI commanded (the AWG has no readback
  in the status topic) — same single-owner assumption as before.
- `galvo_geometry.py` here is the client-side helper (a copy of the one in
  `../microscope`); any external galvo app can reuse the same module.
