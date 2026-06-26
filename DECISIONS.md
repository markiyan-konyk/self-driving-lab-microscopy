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
- **Autofocus is client-side, not a driver action.** It is policy (move Z +
  measure sharpness), composable from the image topic + `stage/jog`, so it stays
  out of the sacred drivers. The gateway runs it as a client routine.
- **The UI stays on the Pi for now, but backed by ROS.** The gateway serves the
  same rich SCOPIO frontend and translates every control to ROS calls, so it
  behaves as before. A future pass moves control programs off-box.
- **Reachable by external programs over LAN/USB now; remote/cloud documented for
  later.** Same-LAN DDS works today (build side-apps now); USB-gadget gives a
  point-to-point link; off-site uses a mesh VPN or a Zenoh bridge — and because
  the contract is frozen, none of that changes the interfaces. See
  `ros2_ws/docs/CONNECTIVITY.md`.
- **The monolith (`microscope/`) is kept, untouched, as the debugging tool** (it
  still has recording and is where tracking will be prototyped). The standalone
  tracking *application* is deferred until after this rewrite.

## 5. Known placeholders (numbers-only fixes later)
- Galvo geometry constants (`PIXELS_PER_VOLT`, `VOLTS_TO_ANGLE`,
  `OPTICAL_THROW_UM`) in `microscope/galvo_geometry.py` are placeholders until
  `galvo_tests/03_precision.py` measures them. The math is wired; only the
  numbers change.
- The ROS graph has **no authentication** yet — keep it on a trusted LAN/VPN.
