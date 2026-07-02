# SCOPIO ROS 2 layer (Phase 2)

Turns the microscope into the **sensor + effectuator** of the self-driving lab.
All planning/decision logic lives on an **external computer** (just another ROS 2
node on the DDS graph); the Pi only senses, publishes, and effectuates.

This is the **ROS layer**; the existing Flask app (`microscope/`) is left intact
and unchanged. Hardware has a single owner at a time — run *either* the ROS stack
*or* the old Flask app, not both.

## Learning / operating this

- **New to ROS 2?** Read [docs/ROS2_TUTORIAL.md](docs/ROS2_TUTORIAL.md) — a
  from-zero tutorial taught through this exact codebase (nodes → interfaces →
  build → launch → Docker), plus a "write your own node" walkthrough.
- **Bringing it up on the Pi?** Follow [docs/BRINGUP.md](docs/BRINGUP.md) — an
  ordered checklist + `scripts/smoke_test.sh` that validates the graph layer by
  layer.

## Packages

| Package | Type | What it is |
|---|---|---|
| `scopio_interfaces` | ament_cmake | The contract: msgs / srvs / **actions** |
| `scopio_microscope` | ament_python | The driver nodes (camera, stage, galvo, calibration, tracker) |

> The **web UI is no longer in this workspace.** It is a separate standalone app
> at repo-root [`../ui`](../ui) — a ROS *client* that owns no hardware and can run
> on the Pi or any other machine. This backend brings up drivers only.

### Nodes (all under the `/scopio` namespace)

> The interface contract is **frozen at v1.0** — see
> [docs/INTERFACES.md](docs/INTERFACES.md) (field-level ICD) and
> [docs/NODES.md](docs/NODES.md) (what each node does + copy-paste connection
> examples). The table below is a summary.

| Node | Publishes | Services | Actions |
|---|---|---|---|
| `camera_node` | `image/compressed`, `camera/state` | `camera/set_controls`, `camera/set_framerate`, `camera/white_balance` | — |
| `stage_node` | `stage/position` | `stage/jog`, `stage/move_abs` | `stage/move_path`, `scan_region` |
| `galvo_node` | `awg/status` | `awg/write`, `awg/query` (raw SCPI passthrough) | — |
| `calibration_node` | `calibration` (latched) | `calibration/set` | — |
| `tracker_node` | `beads` | `tracker/set_active` | — |

> **Recording is not a node** — the camera only streams; the UI gateway records
> the stream to its own local folder. **The galvo node is a raw VISA/SCPI
> passthrough** — geometry (volts→pixels→µm) lives in client code
> (`microscope/galvo_geometry.py`). See [DECISIONS.md](../DECISIONS.md) for why.

Design notes:
- The nodes **reuse** the validated repo code — `microscope/galvo.py`,
  `microscope/tweezer.py`, `viscosity/track.py` (trackpy params) — found via
  `$SCOPIO_REPO` (default `/workspace`). They re-implement the Flask-coupled
  parts (camera config, stage moves) leanly.
- Every node **degrades gracefully**: missing camera/stage/galvo → the node
  still starts and reports `connected=false`, so the graph comes up on a partial
  rig and you can bring hardware online piece by piece.
- Tracking is **on-demand** (`tracker/set_active`) — the Pi only does the heavy
  trackpy work when asked. **trackpy is mandatory** (OpenCV was rejected); the
  live loop uses ROI `tp.locate` with periodic full-frame re-acquisition, same
  parameters as the offline viscosity pipeline.
- **Actions** carry long-horizon goals (a whole stage path, a whole galvo
  waveform, a region scan) so execution doesn't depend on per-step latency.

## Build & run (on the Pi 5)

```bash
cd ros2_ws
# Optional: tell the galvo node which AWG to use (else laser stays "disabled")
export GALVO_RESOURCE="USB0::0x1AB1::0x0642::DG1ZA...::INSTR"
docker compose up --build
```

That brings up the **backend graph (drivers only)**. The web UI is a separate
app — start it from [`../ui`](../ui) (`cd ../ui && docker compose up`), on the Pi
or another machine. From any machine on the LAN (with ROS 2 sourced) you can then
inspect/drive the backend directly:

```bash
ros2 topic list
ros2 topic echo /scopio/stage/position      # steps + micrometres
ros2 topic echo /scopio/camera/state        # settings + real measured fps

# Jog the stage (relative, steps)
ros2 service call /scopio/stage/jog scopio_interfaces/srv/StageJog "{dx: 40, dy: 0, dz: 0}"

# Drive the laser by raw SCPI (the node is instrument-agnostic)
ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite "{command: ':SOURce1:VOLTage:OFFSet 1.25'}"
ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite "{command: ':OUTPut1 OFF'}"

# Turn the bead tracker on, watch the count
ros2 service call /scopio/tracker/set_active std_srvs/srv/SetBool "{data: true}"
ros2 topic echo /scopio/beads

# Long-horizon goal: scan a region
ros2 action send_goal -f /scopio/scan_region scopio_interfaces/action/ScanRegion \
  "{x_min: 0, x_max: 400, y_min: 0, y_max: 400, step: 200, settle_s: 0.5}"
```

### Visualization (portfolio-grade)

Run [Foxglove Studio](https://foxglove.dev/) and connect over the ROS 2 native /
rosbridge connection to see the topics, images and TF live.

## Camera caveat (the one thing to verify on the Pi)

`picamera2`/`libcamera` in a container must match the Pi's kernel/firmware. The
`Dockerfile` installs `python3-picamera2 python3-libcamera python3-kms++` from
apt. If those aren't available on your base image or the camera fails to start:

1. The graph still comes up (camera node logs a warning and idles) — you can
   develop everything else meanwhile.
2. To fix: ensure the host runs a recent Raspberry Pi OS with the camera working
   natively first; then either add the Raspberry Pi apt archive inside the image,
   or `pip install picamera2` against the container's `libcamera`. Keep host and
   container libcamera versions aligned.

## Local development (edit nodes without rebuilding the image)

The image bakes a build at `/ros2_ws`. To iterate on node code live, mount the
workspace and rebuild inside the container instead:

```bash
docker compose run --rm scopio bash
# in the container:
cd /workspace/ros2_ws && colcon build --symlink-install && source install/setup.bash
ros2 launch scopio_microscope microscope.launch.py
```

## Status

Interfaces frozen at **v1.0**. The driver nodes are hardened (camera controls +
framerate/exposure budget, white balance, autofocus action, stage jog/move,
galvo SCPI passthrough, calibration store). The UI is now a **separate standalone
app** ([`../ui`](../ui)) that serves the SCOPIO frontend and records client-side.
Built/structured on a Windows dev box (Python syntax verified); **the ROS build
(`colcon`), Docker image and hardware bring-up must be validated on the Pi.** Galvo geometry constants are
placeholders in `microscope/galvo_geometry.py` — a numbers-only fix once
`galvo_tests/03_precision.py` measures them. See [../DECISIONS.md](../DECISIONS.md)
for the full rationale and [docs/INTERFACES.md](docs/INTERFACES.md) for the
contract.
