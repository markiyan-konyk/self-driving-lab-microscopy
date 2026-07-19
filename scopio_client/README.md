# scopio-client

Python SDK for the SCOPIO microscope API gateway. Everything the microscope
can do -- stage, camera, galvo/laser, calibration, autofocus, scans -- over
plain HTTP/WebSocket with an API key. **No ROS, no Docker, no DDS needed.**

## Install

```bash
pip install -e ./scopio_client        # from the repo root
```

## Use

```python
from scopio_client import Scopio

scope = Scopio("http://<pi-ip>:8000", api_key="<key from generate_api_key.py>")

# Effectuate
scope.stage.jog(dz=100)                       # relative move, Sangaboard steps
scope.stage.move_abs(0, 0, 0)
scope.galvo.write(":OUTPut1 OFF")             # raw SCPI passthrough
scope.camera.set_controls(contrast=1.2)       # partial update, nothing clobbered

# Sense
print(scope.stage.position())                 # latest cached telemetry
scope.subscribe("stage/position", lambda msg, env: print(msg), rate_hz=5)
for jpeg in scope.stream_frames():            # live video, raw JPEG bytes
    open("frame.jpg", "wb").write(jpeg); break

# Long-running actions (blocking, with live feedback)
result = scope.camera.autofocus(z_range=2000, steps=15,
                                on_feedback=lambda fb: print(fb))

# Discover -- ask the microscope what it can do (new nodes appear automatically)
print(scope.interfaces())
```

Generic escape hatch for anything not wrapped yet (e.g. the future temperature
node): `scope.call_service("thermal/set_target", {"celsius": 37.0})`.

Full command manual: `docs/API.md` in the repository. Interactive docs:
`http://<pi-ip>:8000/docs`.
