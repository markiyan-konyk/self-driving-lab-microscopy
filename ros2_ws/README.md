# SCOPIO backend (ROS 2 graph + API gateway)

Turns the microscope into the **sensor + effectuator** of the self-driving
lab. All planning/decision logic lives off-board: external programs (the UI,
galvo_draw, agents) are **API clients** of the gateway — they speak JSON over
HTTP/WebSocket with an API key, never ROS. The Pi only senses, publishes, and
effectuates.

```
cp .env.example .env           # once: which VISA instrument is which (gitignored)
docker compose up -d           # brings up ALL of this:
  scopio   -> the ROS 2 driver graph under /scopio (this workspace)
  camera   -> ../camera_server (picamera2 MJPEG, loopback :8081)
  gateway  -> src/scopio_gateway (HTTP/WS API, LAN :8000, API keys)
```

`.env` is the **only** place the rig's hardware wiring is written down
(`GALVO_RESOURCE`, `TCLAB_RESOURCE`, camera size). Compose reads it by itself —
nothing to export, nothing to remember between sessions — and `temperature_test.py`
reads the same file, so the bench script and the backend can't disagree. Edit it,
then `docker compose up -d` again to apply.

This is the **backend**. (An earlier, pre-ROS Flask monolith that drove the
hardware directly, `microscope/`, has since been deleted — everything it did
is now covered by this stack plus `../ui` and `../galvo_draw`; see
`../docs/DECISIONS.md` §6.)

## Learning / operating this

- **New to ROS 2?** Read [docs/ROS2_TUTORIAL.md](docs/ROS2_TUTORIAL.md) — a
  from-zero tutorial taught through this exact codebase.
- **Bringing it up on the Pi?** Follow [docs/BRINGUP.md](docs/BRINGUP.md) — an
  ordered checklist + smoke tests that validate the stack layer by layer.
- **Writing a client program?** You want [../docs/API.md](../docs/API.md) and
  [../scopio_client](../scopio_client) — not this workspace.

## Packages

| Package | Type | What it is |
|---|---|---|
| `scopio_interfaces` | ament_cmake | The contract: msgs / srvs / **actions** (frozen v1.0) |
| `scopio_microscope` | ament_python | The driver nodes (camera, stage, galvo, temperature, calibration) + the vendored instrument drivers in `scopio_microscope/drivers/` |
| `scopio_gateway` | ament_python | The HTTP/WS API gateway (FastAPI + rclpy) |

### Nodes (all under the `/scopio` namespace)

> The interface contract is **frozen at v1.0** — see
> [docs/INTERFACES.md](docs/INTERFACES.md) (field-level ICD) and
> [docs/NODES.md](docs/NODES.md). The table below is a summary.

| Node | Publishes | Services | Actions |
|---|---|---|---|
| `camera_node` | `image/compressed`, `camera/state` | `camera/set_controls`, `camera/set_framerate`, `camera/white_balance` | `camera/autofocus` |
| `stage_node` | `stage/position` | `stage/jog`, `stage/move_abs` | `stage/move_path`, `scan_region` |
| `galvo_node` | `awg/status` | `awg/call` (any driver method), `awg/write`, `awg/query` (raw SCPI) | — |
| `temperature_node` | `temperature/status` | `temperature/call` (any driver method) | — |
| `calibration_node` | `calibration` (latched) | `calibration/set` | — |

> **Recording is not a node, and neither is image analysis** — the camera only
> streams; clients record locally and do their own detection/tracking off the
> Pi. **The instrument nodes expose a whole driver CLASS** (`awg/call`,
> `temperature/call`): every method the class has is callable by any app, so
> the ROS layer never decides which instrument features are "supported" and
> meaning (volts→pixels→µm, temperature ramps) stays in client code. See
> [docs/DECISIONS.md](../docs/DECISIONS.md) for why.

Design notes:
- **The camera has one owner: `../camera_server`** (picamera2 can't run in
  this Ubuntu container). `camera_node` runs in **bridge mode**: it ingests
  the camera server's MJPEG over loopback, republishes `image/compressed`,
  forwards the camera services, and runs the autofocus action on the ingested
  frames. The frozen interface behaves identically either way.
- Every node **degrades gracefully**: missing camera/stage/galvo → the node
  still starts and reports `connected=false`, so the graph comes up on a
  partial rig (or a hardware-less dev box) and you bring things online piece
  by piece.
- **The backend never analyses the image.** It streams frames and moves
  hardware; bead detection, tracking and every decision that depends on them
  run on the client side, off the Pi (`../viscosity`, `../viscosity_agent`).
- **Actions** carry long-horizon goals (a whole stage path, a region scan,
  an autofocus sweep) so execution doesn't depend on per-step latency.
- The gateway maps the graph to JSON **generically** (introspected from the
  live graph) — a new node needs zero gateway changes to become remotely
  usable; it just shows up in `GET /api/v1/interfaces`.

## Build & run (on the Pi)

```bash
cd ros2_ws
# Optional: pin the AWG (else laser stays "disabled"):
echo 'GALVO_RESOURCE=USB0::0x1AB1::0x0642::DG1ZA...::INSTR' > .env
python3 scripts/generate_api_key.py <client-name>     # per client app/person
docker compose up -d --build
```

Verify: `curl http://127.0.0.1:8000/api/v1/health`. Then drive it from any
machine — see [../docs/API.md](../docs/API.md). For direct graph poking during
backend development (`ros2 topic echo ...`), see
[docs/CONNECTIVITY.md](docs/CONNECTIVITY.md).

## Local development

**No-hardware dev stack** (works on a laptop, even under Docker Desktop):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
python3 scripts/smoke_test_api.py --url http://localhost:8000
```

**Edit nodes live** without rebuilding the image:

```bash
docker compose run --rm scopio bash
# in the container:
cd /workspace/ros2_ws && colcon build --symlink-install && source install/setup.bash
ros2 launch scopio_microscope microscope.launch.py
```

## Status

Interfaces frozen at **v1.0**; API gateway + client SDK added (branch
`remake`). The camera runs as its own compose service (`../camera_server`,
Debian bookworm + RPi apt archive) with a systemd fallback — validating
picamera2-in-container on the real Pi is the one open hardware risk (see
BRINGUP step "camera gate"). Galvo geometry constants are placeholders in
`microscope/galvo_geometry.py` pending `galvo_tests/03_precision.py`.
