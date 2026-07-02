# galvo_draw — draw with the laser

A standalone app that turns a drawing (or a draggable wireframe cube) into laser
motion. You draw on a blank canvas; the galvo traces it as a continuous vector
image. It is **just another ROS client** — it talks to the SCOPIO backend only
through the `awg/write` passthrough, exactly like the UI.

```
browser canvas ──path──► galvo_draw ──arbitrary-waveform SCPI──► /scopio/awg/write
                                                                  (AWG loops it in hardware)
```

## Why it works this way
- **The laser can't be blanked**, so we can't raster (CRT-style) — you'd see
  every scan line. Instead it's a **vector display**: one continuous closed path
  the beam retraces fast enough to look solid (persistence of vision). Separate
  pen strokes are joined by visible travel lines — unavoidable with an always-on
  beam.
- For flicker-free output we **don't** stream points over ROS (too slow). We
  upload the whole path once as an **arbitrary waveform** (X→CH1, Y→CH2, phase
  aligned) and let the AWG loop it in hardware at the chosen rate.
- **Amplitude (Vpp)** scales the waveform → the size of the drawing.
- (Fourier/epicycles is a nice way to *describe* a closed curve, but since we can
  upload the sampled path directly, it isn't needed. The path is resampled to
  2048 points by arc length in `app.py`.)

## Run
Backend first (`../ros2_ws`), then:
```bash
cd galvo_draw
docker compose up --build              # → http://localhost:8090
#   native: pip install -r requirements.txt && python3 app.py   (after sourcing ROS + scopio_interfaces)
```

## Using it
- **Draw mode**: click-drag to draw; release to send. Multiple strokes allowed.
- **Cube mode**: drag to rotate a wireframe cube; it updates live.
- **Amplitude** slider = size (Vpp). **Loop rate** = how fast the AWG retraces
  (higher = less flicker; too high may exceed galvo bandwidth and round corners).
- **Enable laser output** actually drives the beam (off by default for safety).
- **Test link** sends one known-good DC offset to confirm the ROS→AWG pipe before
  trusting the arbitrary-waveform path.
- **Stop** parks the beam as a single dot (the laser stays on — it can't blank).

## ⚠ One thing to verify on real hardware
The DG1022Z arbitrary-waveform command sequence lives entirely in
[`galvo_scpi.py`](galvo_scpi.py). The tokens follow the Rigol DG1000Z guide but
may need a one-line tweak for your firmware. If **Test link** moves the beam but a
drawing doesn't appear, fix the SCPI there — the app and the ROS layer don't
change. Galvo size/orientation also depends on the (placeholder) calibration in
`galvo_geometry.py`.
