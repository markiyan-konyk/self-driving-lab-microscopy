# SCOPIO — Decisions & Rationale Log

A running record of *why* the project is built the way it is, in roughly the
order the decisions were made. The point is to preserve the reasoning, not just
the result — so future work (and future people) understand the trade-offs.

---

## 1. The monolith UI (microscope/) — cleanup and polish

- **Removed the ML / particle-tracking from the live app.** Tracking will be done
  separately, *after* recording the footage. The app only needs a single colour
  video stream + recording, not live detection. (The dual stream became one; the
  tracker view and settings were removed.)
- **Auto white balance uses the camera's own hardware AWB**, not a hand-rolled
  grey-world loop. The old loop converged wrong (image briefly went white, then
  reverted to blue). Now: enable AWB, let it settle, read the colour gains it
  chose, freeze them. Known-good manual reference: R≈2.4, B≈2.5, G=1.
- **One frame-rate slider; exposure is automatic.** The camera went dark above 30
  fps because the frame period caps exposure. Fix: keep a constant *exposure
  budget* = `exposure_us × analogue_gain`; raising fps shortens exposure and
  raises gain to hold brightness. No manual exposure control is exposed.
- **Recording filename reflects the *actual* length** (measured on stop), not the
  planned duration; and the video scales to fill its pane (no black bars — the
  leftover area is page background).
- **Movement:** press-and-hold continuous jog (mouse + keyboard), up/down fixed,
  editable step-size boxes.

## 2. Branding — SCOPIO

- The product is **SCOPIO**, "MatterLab Automated Microscope" (UofT Matter Lab).
  Dark grey-green theme, emerald accent. The MatterLab dot-triangle logo is an
  SVG at `microscope/frontend/logo.svg`, inlined so its dots can animate. If the
  official lab SVG is dropped in there (root `class="ml-logo"`, circles
  `class="lg-dot"`) it is used automatically.
- Rich UI: left recordings library (delete/preview/rename, real duration+fps via
  ffprobe), centre video with a telemetry HUD (X/Y/Z + real fps) and a
  measurement/scale-bar overlay, right controls (collapsible camera section),
  plus a login splash and a phone control mode.
- **Real fps** is measured from the sensor (`FrameDuration`), never assumed from
  the requested value (asking 120 may only yield ~89).

## 3. Why ROS 2 at all

The monolith drives the camera/stage/galvo **directly, in-process**. That means
only one program can own the hardware at a time, and nothing external can reuse
it. The goal of a *self-driving lab* is for many programs — a UI, a tracker, a
galvo app, an autonomous controller — to share the rig. ROS 2 gives exactly
that: driver nodes own the hardware and expose it over a typed graph that any
client can join. **ROS owns the hardware; the UI becomes one more ROS client.**

## 4. The ROS migration decisions (this rewrite)

- **Harden & freeze the existing scaffold, don't rebuild.** The `marki`-branch
  nodes (camera/stage/galvo/tracker + `scopio_interfaces` + gateway + Docker)
  were good. We completed and reshaped them and declared `scopio_interfaces`
  **v1.0 frozen** — a documented contract (see `ros2_ws/docs/INTERFACES.md`) so
  external authors know the inputs/outputs and can rely on them.
- **Recording is a client concern; the Pi only streams.** The Pi 4 (4 GB) must
  not take extra load, and the footage is small (~600×400), so the camera node
  *streams* frames and never saves. The **UI program records to its own local
  folder** — on the Pi if the UI runs there, on another machine if it runs there.
  (Removed the node-side `recording/set` + `recording/status`.)
- **The galvo node is a raw VISA/SCPI string passthrough.** External programs
  send the instrument's command strings; the node just relays them. Two reasons:
  (1) if we swap the AWG later, **the ROS code does not change** — it stays
  sacred; (2) one string expresses DC / sine / square / arbitrary waveforms and
  output on/off without a combinatorial typed API. The laser *geometry*
  (volts→pixels→µm, home, jog) therefore lives in **client** code
  (`microscope/galvo_geometry.py`), reusable by the UI and any galvo app.
  (Removed the typed `SetLaser` / `ZeroTweezers` / `RunGalvoWaveform`.)
- **Stage and camera stay *typed* (not passthrough).** They must hold state the
  node has to understand: the stage tracks absolute position from accumulated
  relative moves (so it knows where it is "in the map"), and the camera applies
  the fps→exposure budget math. A passthrough couldn't do that.
- **Position "in the map" = steps + micrometres**, derived from the persisted
  calibration. A dedicated `calibration_node` owns µm/px and steps/µm, persists
  them to disk (survives `main.py`/relaunch), and publishes them **latched** so
  every client — including the stage node — gets them on join.
- ~~Autofocus is client-side, not a driver action.~~ **Superseded in §6:**
  autofocus is now a backend action (`camera/autofocus`) so any client gets it
  for free, without reimplementing the sweep/backlash logic.
- ~~The UI stays on the Pi for now.~~ **Superseded in §6:** the UI is now an
  external client (the Pi has no spare compute, and multiple people may run
  their own UI at once).
- **Reachable by external programs over LAN/USB now; remote/cloud documented for
  later.** Same-LAN DDS worked at this point in the project (see §6 for what
  replaced it as the client-facing path); USB-gadget gives a point-to-point
  link; off-site uses a mesh VPN or a Zenoh bridge for the *DDS graph itself*,
  which stays Pi-internal now anyway. See `ros2_ws/docs/CONNECTIVITY.md`.
- ~~The monolith (`microscope/`) is kept, untouched, as the debugging tool.~~
  **No longer true:** `microscope/` was deleted once `ui/` and `galvo_draw/`
  fully replaced it (see §6). The standalone tracking *application* is still
  deferred; `viscosity/` (the offline analysis pipeline `tracker_node` borrows
  parameters from) remains.

## 5. Known placeholders (numbers-only fixes later)
- Galvo geometry constants (`PIXELS_PER_VOLT`, `VOLTS_TO_ANGLE`,
  `OPTICAL_THROW_UM`) in `microscope/galvo_geometry.py` are placeholders until
  `galvo_tests/03_precision.py` measures them. The math is wired; only the
  numbers change.

## 6. The API-gateway rewrite (branch `remake`)

Same-LAN DDS (§4) turned out to be impractical in practice: every client
needed ROS 2 + Docker + a matching `ROS_DOMAIN_ID` + (on Windows) WSL2 with
mirrored networking + Fast-DDS unicast peer files + firewall holes for UDP
discovery — for a lab where people just want to open a URL. It also had **no
authentication**: anything on the network/domain could command the hardware.

- **Wrap the graph in an HTTP/WebSocket API gateway (`scopio_gateway`), gated
  by API keys.** External programs now speak plain JSON over a normal
  request/response + WebSocket protocol — no ROS install, no Docker, no DDS
  config, on any OS. The gateway is a new ROS node (rclpy) that also runs
  FastAPI/uvicorn in the same process, so it's just another package in this
  workspace, not a separate service to keep in sync.
- **Generic over curated.** The gateway maps `POST /api/v1/service/{name}`
  and WebSocket topic/action ops onto the live graph via ROS introspection
  (`rosidl_runtime_py`), not hand-written per-endpoint code. This means the
  frozen `scopio_interfaces` contract (§4) still pays off exactly as
  intended: a brand-new node (the planned temperature/heating stack) becomes
  remotely callable and self-documenting (`GET /api/v1/interfaces`) the
  moment it launches — zero gateway changes.
- **The API key is just the lock; the gateway is the only door.** Keys live
  in `ros2_ws/secrets/api_keys.json` (gitignored, hot-reloaded — revoke
  without a restart). The camera server and the raw DDS graph never face the
  LAN directly anymore; only port 8000 does.
- **Autofocus moved from client policy to a backend action.** With the
  camera reachable in-graph again (via the bridge mode below), there's no
  reason to keep re-implementing the Z-sweep/backlash/sharpness logic in
  every client — one correct implementation, callable by anyone.
- **The camera gets its own always-on container instead of a manually-run
  script.** `pi_camera_server.py` (still the sole picamera2 owner — it can't
  run in the Ubuntu ROS container, same reasoning as before) moved to
  `camera_server/` and became its own `docker compose` service (Debian
  bookworm + the Raspberry Pi apt archive), with `restart: unless-stopped`
  and a systemd-unit fallback if libcamera doesn't behave in that container
  on a given Pi/kernel combo. `camera_node` gained a **bridge mode**: when
  picamera2 isn't importable (always true in-container) it ingests the
  camera server's MJPEG over loopback and republishes it on
  `image/compressed`, so the rest of the graph (tracker, autofocus) can't
  tell the difference from native mode.
- **The UI moves off the Pi.** Once it's just an HTTP client, there's no
  reason to run it on the Pi's limited compute, and running it externally
  lets multiple people each run their own copy against the same microscope
  at once.
- **`microscope/` (the legacy monolith) is deleted, not archived in-repo.**
  `ui/` and `galvo_draw/` had already fully absorbed everything useful from
  it (camera control math, white balance, galvo geometry — see §4), so it was
  pure dead weight once the last debugging use for it passed. Its git history
  still has it if anyone needs to dig it up. `viscosity/` and `galvo_tests/`
  are unrelated (offline analysis / hardware bench scripts) and stay.
- See `docs/API.md` for the resulting command manual and `ros2_ws/docs/CONNECTIVITY.md`
  for what's left of the DDS-networking story (now Pi-internal only).
