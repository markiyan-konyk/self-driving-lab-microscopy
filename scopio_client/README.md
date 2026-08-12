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
scope.temperature.setpoint(37.0)
scope.temperature.output(True)                # nothing heats while the TEC is off
scope.laser.on()                              # the relay on the Pi's GPIO pin

# Sense
print(scope.stage.position())                 # latest cached telemetry
print(scope.temperature.temperature())        # live read from the instrument
print(scope.galvo.query("*IDN?"))             # SCPI query -> reply string
print(scope.laser.is_on())                    # None means UNKNOWN, never "off"
scope.subscribe("stage/position", lambda msg, env: print(msg), rate_hz=5)
for jpeg in scope.stream_frames():            # live video, raw JPEG bytes
    open("frame.jpg", "wb").write(jpeg); break

# Long-running actions (blocking, with live feedback)
result = scope.camera.autofocus(z_range=2000, steps=15,
                                on_feedback=lambda fb: print(fb))

# Discover -- ask the microscope what it can do (new nodes appear automatically)
print(scope.interfaces())
```

### Reaching past the sugar

Two escape hatches cover everything the convenience methods don't:

```python
scope.call_service("calibration/set", {"um_per_px": 0.42})   # any ROS service
scope.send_goal("scan_region", {...})                        # any ROS action

# The galvo and temperature nodes each expose a whole driver class. Call any
# method on it, and ask the instrument itself what it has:
scope.temperature.call("aux_temperature")
scope.galvo.call("sinupdate", 1, freq=50)
[m["name"] for m in scope.galvo.methods()]
```

Every failure -- unreachable Pi, HTTP error, rejected SCPI, aborted action --
raises `ScopioError`. Close with `scope.close()` or use it as a context manager;
that shuts down the background WebSocket thread.

Full command manual: `docs/API.md` in the repository. Interactive docs:
`http://<pi-ip>:8000/docs`.

## Test

```bash
pip install fastapi uvicorn                     # the mock gateway
python scopio_client/test_scopio_client.py      # no microscope needed
```

Starts a mock gateway on localhost and drives the SDK at it: HTTP calls and
errors, partial updates, SCPI success/failure, MJPEG frame splitting, and the
WebSocket subscribe / action / reconnect paths.
