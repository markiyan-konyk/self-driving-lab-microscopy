# SCOPIO on-Pi bring-up checklist

Do these **in order**. Each step isolates one layer, so when something breaks
you know exactly where. The nodes degrade gracefully (no camera/stage/galvo =
node still runs, reports `connected=false`), so you can verify the graph and
the API *before* worrying about hardware.

Legend: 🖥️ = on the Pi, 💻 = on another machine on the same network.

---

## 0. Prerequisites (before any ROS)

🖥️ Confirm the *host* basics first — ROS can't fix a broken camera:

- [ ] `rpicam-hello -t 2000` shows a camera preview (libcamera works on the host).
- [ ] `docker --version` and `docker compose version` work; `sudo systemctl
      enable docker` so the stack survives reboots.
- [ ] Stage (Sangaboard), galvo AWG (DG1022Z) and — if you're using it — the
      TC LAB temperature controller are plugged in.
- [ ] **Write the rig down once**, in `ros2_ws/.env` (compose reads it
      automatically; it is gitignored because it describes *this* Pi):

      ```bash
      cd ros2_ws && cp .env.example .env       # then edit it
      lsusb                                    # sanity: the boxes are seen at all
      ```

      Both instrument lines are **optional**: each node auto-discovers by USB
      vendor id and only ever opens its own instrument. Set one to pin it, or
      when the instrument is on Ethernet (those can never be discovered):

      ```bash
      docker compose down
      python3 scripts/list_instruments.py
      ```
- [ ] Generate at least one API key:
      `python3 scripts/generate_api_key.py laptop` (note the printed key).

> If `rpicam-hello` fails, fix that first. A broken host camera will never
> work in the container either.

---

## 1. Build the images 🖥️

```bash
cd ros2_ws
docker compose build
```

- [ ] Build completes. The risky lines are `colcon build` (compiles the
      interfaces + nodes + gateway) and the `camera` image's
      `python3-picamera2` install from the Raspberry Pi apt archive.

## 2. Bring up the stack 🖥️

```bash
docker compose up -d
docker compose logs -f       # watch each service announce itself
```

Run the graph smoke test **inside the container**:

```bash
docker compose exec scopio bash -lc \
  "source /opt/ros/jazzy/setup.bash && source /ros2_ws/install/setup.bash && bash /workspace/ros2_ws/scripts/smoke_test.sh"
```

- [ ] All driver nodes present: `calibration_node camera_node stage_node
      galvo_node temperature_node` (+ the `gateway` node).
- [ ] Topics listed under `/scopio/...`; `stage/position`, `camera/state`,
      `awg/status`, `temperature/status`, `calibration` are *publishing* (even
      with no hardware).

If a node is **MISSING**, it crashed — `docker compose logs` shows the
traceback. Fix that before going on.

## 3. Camera gate 🖥️ (the one go/no-go branch)

- [ ] `docker logs scopio-camera` shows the server starting.
- [ ] Validate picamera2 in-container:
      `docker compose run --rm camera python3 -c "from picamera2 import Picamera2; print(Picamera2.global_camera_info())"`
- [ ] `curl http://127.0.0.1:8081/controls` returns JSON.
- [ ] `ros2 topic hz /scopio/image/compressed` (inside the scopio container)
      shows ~15 Hz — camera_node's bridge mode is ingesting and republishing.

**If the container camera fails** (libcamera/kernel mismatch): switch to the
systemd fallback — identical HTTP surface, nothing downstream changes:

```bash
docker compose stop camera
cd ../camera_server && sudo ./install_systemd.sh
curl http://127.0.0.1:8081/controls
```
(Comment the `camera` service out of `docker-compose.yml` afterwards.)

## 4. Gateway 🖥️

- [ ] `curl http://127.0.0.1:8000/api/v1/health` →
      `{"ok": true, "ros_ok": true, "camera_ok": true, "auth_configured": true}`.
- [ ] No key → 401: `curl -i http://127.0.0.1:8000/api/v1/status`
- [ ] With key → snapshot:
      `curl -H "X-API-Key: $KEY" http://127.0.0.1:8000/api/v1/status`

## 5. From the client machine 💻

```bash
python3 ros2_ws/scripts/smoke_test_api.py --url http://<pi-ip>:8000 --key $KEY
```

- [ ] All checks pass (with hardware attached it also does a ±0-step jog
      round-trip and an `*IDN?` galvo query).
- [ ] Video: open `http://<pi-ip>:8000/api/v1/stream.mjpg?api_key=$KEY` in a
      browser — live frames.

## 6. Stage 🖥️/💻

- [ ] `/api/v1/status` shows `stage/position ... connected: true`.
- [ ] Jog and watch numbers move:
      `curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"dz": 100}' http://<pi>:8000/api/v1/service/stage/jog`

## 7. Galvo / laser 💻 (uncalibrated is fine)

- [ ] `awg/status` shows `connected: true` in `/api/v1/status`.
- [ ] `*IDN?` answers:
      `curl ... -d '{"command": "*IDN?"}' .../api/v1/service/awg/query`
- [ ] Point it: `-d '{"command": ":SOURce1:VOLTage:OFFSet 0.25"}'` on
      `service/awg/write`; output on/off with `":OUTPut1 ON"` / `":OUTPut1 OFF"`.
- [ ] The class surface answers too:
      `-d '{"method": "list_methods"}'` on `service/awg/call`.

## 7b. Temperature controller 💻

- [ ] `temperature/status` shows `connected: true` in `/api/v1/status`. If it's
      false with the controller plugged in, `docker compose logs scopio | grep
      "TC10 LAB"` says what it tried. Check USB permissions
      (`ros2_ws/udev/99-scopio-instruments.rules`) and, for an Ethernet unit,
      `TCLAB_RESOURCE` in `ros2_ws/.env`.
- [ ] Read it: `curl ... -d '{"method": "temperature"}' .../api/v1/service/temperature/call`
- [ ] Drive it: `-d '{"method": "set_setpoint", "args": "[25.0]"}'`, then
      `-d '{"method": "output", "args": "[true]"}'` and watch `temperature`
      move on the status topic.
- [ ] Turn it back off when done: `-d '{"method": "output", "args": "[false]"}'`.

## 8. Apps + autofocus 💻

- [ ] `ui/run_ui.py` against the Pi: video, sliders, white balance, stage
      arrows, **Autofocus** (this exercises the backend action end-to-end:
      camera frames + stage Z sweep).
- [ ] `galvo_draw/app.py`: **Test link** draws the sine circle.

## 9. Reboot test 🖥️

- [ ] `sudo reboot`, wait, then from the laptop:
      `curl http://<pi-ip>:8000/api/v1/health` — everything returns with zero
      SSH sessions (docker enabled + `restart: unless-stopped`, or the systemd
      camera unit).

---

## Quick triage

| Symptom | Likely cause |
|---|---|
| Node MISSING in smoke test | import/crash — `docker compose logs scopio` |
| Topic exists but no messages | hardware absent/failed (node degraded gracefully) |
| `camera_ok: false` | camera service down — step 3 (container vs systemd) |
| `curl 127.0.0.1:8081/controls` → 503 `{"cameras": []}` | picamera2/libcamera are fine; **no sensor visible**. Check the host first (`rpicam-hello --list-cameras`); if the host sees it, the container's libcamera does not match the host kernel — take the systemd fallback in step 3. |
| Black video but `camera_ok: true` | stream proxy vs camera: `curl 127.0.0.1:8081/stream.mjpg | head -c 100` on the Pi |
| `401` from gateway | key not in `secrets/api_keys.json` (regenerate; hot-reloaded) |
| `504` on service calls | node up but hardware not answering (cables, `GALVO_RESOURCE`) |
| `awg/status connected: false` | `GALVO_RESOURCE` unset/wrong in `.env`, or USB perms |
| `temperature/status connected: false` | Controller off/unplugged, USB perms, or an Ethernet unit with no `TCLAB_RESOURCE` |
| Every SCPI call times out but `lsusb` shows the instrument | Its USBTMC session is stalled. Power-cycle the instrument itself — a USB replug does not reset a self-powered box, and neither does a port reset. |
| Edited `.env`, nothing changed | `docker compose up -d` again — env vars are baked in at container creation |
| `list_resources()` doesn't show an instrument | Run `python3 instrument_scan.py` (host **and** `docker compose exec scopio python3 /workspace/instrument_scan.py`). A device whose USB interface class is CDC/vendor is a **virtual COM port**: it can only ever be an `ASRL/dev/tty…::INSTR` resource, never `USB…::INSTR`, and USB auto-discovery skips it by design. Missing pyserial hides serial instruments entirely. |
| Temperature reads but never moves | TEC output off (`output`, `[true]`), or the rear Remote-Enable input is gating it |
