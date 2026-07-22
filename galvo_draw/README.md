# galvo_draw — draw with the laser

A standalone app that turns a drawing (or a draggable wireframe cube) into laser
motion. You draw on a blank canvas; the galvo traces it as a continuous vector
image. It is **just another API client** — like `ui/` and `viscosity_agent/`, it
talks to the SCOPIO microscope only through the `awg/write` passthrough on the
API gateway. **No ROS, no Docker, no DDS, no pyvisa here.**

```
browser canvas ──strokes──► galvo_draw ──arbitrary-waveform SCPI──► gateway ──► awg/write
                                                                    (AWG loops it in hardware)
```

## Run
Backend first (on the Pi: `cd ros2_ws && docker compose up -d`), then on any
machine that can reach the Pi:
```bash
cd galvo_draw
pip install -r requirements.txt        # includes the scopio_client SDK
cp .env.example .env                   # then edit .env: put the Pi's IP + API key
python app.py                          # → http://localhost:8090
```
`.env` holds the two things you fill in:
```
SCOPIO_URL=http://<pi-ip>:8000
SCOPIO_API_KEY=<key>
```
Get a key on the Pi: `python3 ros2_ws/scripts/generate_api_key.py galvo_draw`.
Everything else (loop rate, size, resolution, dim-travel) is in `config.yaml`.

## Why it works this way
- **The laser can't be blanked**, so we can't raster (you'd see every scan line).
  Instead it's a **vector display**: one continuous closed path the beam retraces
  fast enough to look solid (persistence of vision).
- For flicker-free output we **don't** stream points over ROS (too slow). We
  upload the whole path once as an **arbitrary waveform** (X→CH1, Y→CH2, phase
  aligned) and let the AWG loop it in hardware at the chosen rate.
- **Amplitude (Vpp)** scales the waveform → the size of the drawing. No pixel/µm
  calibration is involved: the path is normalized to [-1, 1] and Vpp maps that to
  volts.

## Handling the always-on beam (the gaps between strokes)
Separate strokes are joined by travel lines — unavoidable with a beam that can't
switch off. Because the AWG steps through the uploaded samples at a constant
rate, **beam speed = sample density**: pack many samples onto a stroke and the
beam crawls (bright); put only a few on a jump and it whips across (faint).

- **Dim travel (default, `dim_travel: true`)** — strokes get most of the samples
  (bright, evenly lit); each connector gets only `travel_points` samples, so the
  beam races across the gap and it fades. This is the "slow-draw / fast-travel"
  trick. Toggle it live in the UI.
- **Uniform (`dim_travel: false`)** — one arc-length loop through everything;
  travel lines are as bright as the drawing.
- For a genuinely blank result, keep it to a **single stroke** (one-stroke
  drawings), or aim off-canvas so the beam leaves the sample during travel.

## Using it
- **Draw mode**: click-drag to draw; multiple strokes allowed.
- **Cube mode**: drag to rotate a wireframe cube; it updates live.
- **Amplitude** = size (Vpp). **Loop rate** = retrace rate (higher = less
  flicker; too high and a mechanical galvo can't follow — the figure rounds/
  distorts, so tune `loop_hz_*` and `n_points` to your galvo's bandwidth).
- **Dim travel lines** = the speed-modulation toggle above.
- **Enable laser output** actually drives the beam (off by default for safety).
- **Test link** sends a known-good slow circle (the proven sine path) to confirm
  the ROS→AWG→galvo pipe before trusting the arbitrary-waveform path.
- **Stop** parks the beam as a single dot (the laser stays on — it can't blank).

## Where the hardware knowledge lives
All DG1022Z SCPI is in [`galvo_scpi.py`](galvo_scpi.py) — verified against the
RIGOL DG1000Z Programming Guide (the `:SOURce<n>:DATA VOLATILE` float path,
frequency-mode arb, High-Z output, phase sync). The ROS `galvo_node` is a dumb
string passthrough, so to target a different AWG you change only that one file.
Path resampling / speed modulation is in [`galvo_paths.py`](galvo_paths.py). If
**Test link** moves the beam but a drawing doesn't appear, the fault is isolated
to the arbitrary-waveform path in `galvo_scpi.py`.
