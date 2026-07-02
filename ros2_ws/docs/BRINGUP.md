# SCOPIO on-Pi bring-up checklist

> ⚠️ **Some commands below predate the v1.0 interface freeze** (e.g.
> `laser/state`, `recording/status`, `tweezers/zero`). The authoritative,
> current contract is [INTERFACES.md](INTERFACES.md); connection examples that
> match the frozen interfaces are in [NODES.md](NODES.md). Update steps here to
> those names as you go.

Do these **in order**. Each step isolates one layer, so when something breaks
you know exactly where. The nodes degrade gracefully (no camera/stage/galvo =
node still runs, reports `connected=false`), so you can verify the ROS graph
*before* worrying about hardware.

Legend: 🖥️ = on the Pi, 💻 = on another machine on the same LAN.

---

## 0. Prerequisites (before any ROS)

🖥️ Confirm the *host* basics first — ROS can't fix a broken camera:

- [ ] `rpicam-hello -t 2000` shows a camera preview (libcamera works on the host).
- [ ] `docker --version` and `docker compose version` work.
- [ ] Stage (Sangaboard) and galvo (DG1022Z) are plugged in.
- [ ] Find the galvo's VISA address: run `python galvosetup.py` (from the repo)
      and note the `USB0::...` string. You'll pass it as `GALVO_RESOURCE`.

> If `rpicam-hello` fails, fix that first. The container's libcamera must talk to
> the same camera stack; a broken host camera will never work in the container.

---

## 1. Build the image 🖥️

```bash
cd ros2_ws
docker compose build
```

- [ ] Build completes. The risky line is `colcon build` (compiles the interface
      package). If it fails, the error names the offending `.msg`/`.srv`/`.action`.
- [ ] If `python3-picamera2` can't be apt-installed on the base image, see the
      "Camera caveat" in `ros2_ws/README.md`. The build can still succeed — the
      camera just degrades at runtime.

## 2. Bring up the graph (hardware-agnostic) 🖥️

```bash
GALVO_RESOURCE="USB0::0x1AB1::..." docker compose up
# (or `-d` to detach; then `docker compose logs -f`)
```

Watch the logs. You want to see each node announce itself; warnings like
"Camera unavailable … idling" are fine at this stage.

In a second terminal, run the smoke test **inside the container**:

```bash
docker compose exec scopio bash -lc "source /entrypoint.sh true; bash /workspace/ros2_ws/scripts/smoke_test.sh"
```

- [ ] All five backend nodes present: `calibration_node camera_node stage_node
      galvo_node tracker_node`. (The UI is a separate app — not a node here.)
- [ ] Topics listed under `/scopio/...`.
- [ ] `stage/position`, `camera/state`, `awg/status`, `calibration` are
      *publishing* (they publish even with no hardware).

If a node is **MISSING**, it crashed — `docker compose logs` shows the traceback.
Fix that before going on.

## 3. Camera 🖥️

- [ ] `ros2 topic hz /scopio/image/compressed` shows ~15 Hz.
- [ ] Start the UI app (separate): `cd ../ui && docker compose up`, then browse
      to `http://<pi-ip>:8080` — you should see live video.
- [ ] (Optional) start/stop a recording from the UI; confirm an `.mp4` appears in
      `ui/recordings/` on whatever machine runs the UI.

If video is black: the camera node logged a warning — revisit the Camera caveat.

## 4. Stage 🖥️

- [ ] `ros2 topic echo /scopio/stage/position` shows `connected: true`.
- [ ] Send a tiny path and watch the numbers move:
```bash
ros2 action send_goal /scopio/stage/move_path scopio_interfaces/action/MoveStagePath \
  "{points: [{x: 40, y: 0, z: 0}, {x: 0, y: 0, z: 0}], settle_s: 0.5}"
```

## 5. Galvo / laser 🖥️ (uncalibrated is fine)

- [ ] `ros2 topic echo /scopio/awg/status` shows `connected: true`.
- [ ] Point it (raw SCPI — the node is instrument-agnostic):
      `ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite "{command: ':SOURce1:VOLTage:OFFSet 0.25'}"`.
- [ ] Output on/off: `ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite "{command: ':OUTPut1 ON'}"`.
- [ ] Run a waveform — same passthrough, a different SCPI string, e.g.
```bash
ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite \
  "{command: ':SOURce1:APPLy:SINusoid 2,1.0,0'}"
```
      (Geometry/waveform meaning lives in client code, e.g. `GalvoClient` in
      `../ui/galvo_geometry.py`.)

## 6. Tracker (the real-time question) 🖥️

```bash
ros2 service call /scopio/tracker/set_active std_srvs/srv/SetBool "{data: true}"
ros2 topic hz   /scopio/beads      # <-- the achievable tracking rate
ros2 topic echo /scopio/beads      # counts + positions
```

- [ ] `beads` publishes; `count` is sane for what's under the scope.
- [ ] Note the **Hz** — this is the empirical real-time answer. If it's low,
      tune `params.yaml` (`acquire_every_n`, `roi_size`, `percentile`) or apply
      the raw-image optimisation (see README "real-time" note).

## 7. The external decision computer 💻

Prove the multi-machine story:

```bash
# On the other machine (ROS 2 installed, same network, same ROS_DOMAIN_ID):
ros2 topic list                       # should show /scopio/... from the Pi
ros2 topic echo /scopio/beads         # receiving the Pi's sensor data remotely
ros2 action send_goal /scopio/stage/move_path ...   # commanding it remotely
```

- [ ] The Pi's topics appear on the other machine with no extra config
      (host-network DDS auto-discovery).
- [ ] (Optional) Foxglove Studio 💻 connects and shows everything live.

---

## Quick triage

| Symptom | Likely cause |
|---|---|
| Node MISSING in smoke test | import/crash — `docker compose logs` |
| Topic exists but no messages | hardware absent/failed (node degraded gracefully) |
| Black video | libcamera/picamera2 in container (README caveat) |
| `laser/state connected:false` | `GALVO_RESOURCE` unset/wrong, or USB perms |
| Other machine sees nothing | different `ROS_DOMAIN_ID`, or not host-network |
| Tracker Hz too low | tune params / raw-image optimisation |
