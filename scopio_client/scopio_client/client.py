"""The Scopio client: one object per microscope.

    scope = Scopio("http://10.42.0.1:8000", api_key="...")

Generic surface (works for every current AND future capability):
    scope.call_service("stage/jog", {"dx": 100})     # any ROS service, JSON in/out
    scope.subscribe("stage/position", cb)            # any (small) topic, live
    scope.send_goal("camera/autofocus", {...})       # any action, blocking
    scope.interfaces()                               # ask the microscope what it has

Convenience namespaces (thin sugar over the generic surface):
    scope.stage.jog(dx=..., dy=..., dz=...) / move_abs(x, y, z) / position()
    scope.camera.get_controls() / set_controls(...) / set_framerate(fps)
                 / white_balance() / autofocus(...)
    scope.galvo.write(cmd) / write_all(cmds) / query(cmd) / status()
    scope.temperature.temperature() / setpoint(c) / output(on) / status()
    scope.laser.on() / off() / set(bool) / is_on()
    scope.calibration.get() / set(um_per_px=...)
    scope.stream_frames()                            # generator of JPEG bytes

Instrument classes: the galvo and temperature nodes each own a driver CLASS and
expose every one of its methods over one service. Reach anything the sugar
above doesn't cover with .call(), and ask the instrument what it has:
    scope.temperature.call("set_pid", 1.2, i=0.4)
    scope.galvo.call("apply_sine", 1000, 2.0, channel=2)
    [m["name"] for m in scope.temperature.methods()]

NaN sentinels: calibration/set (and the ROS camera/set_controls service) treat
NaN as "leave unchanged". The convenience methods pre-fill JSON null (-> NaN)
for every field you don't pass, so partial updates are safe by default.
"""

import json

import requests

from .errors import ScopioError
from .stream import iter_jpegs
from .ws import WsManager

DEFAULT_TIMEOUT = 15.0


class Scopio:
    def __init__(self, base_url, api_key, timeout=DEFAULT_TIMEOUT):
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url      # "10.42.0.1:8000" is a fine URL to type
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self._http = requests.Session()
        self._http.headers["X-API-Key"] = api_key
        # http -> ws, https -> wss (keys are hex, so no query escaping needed).
        ws_url = "ws" + self.base_url[4:] + f"/api/v1/ws?api_key={api_key}"
        self._ws = WsManager(ws_url)
        self.stage = _Stage(self)
        self.camera = _Camera(self)
        self.galvo = _Galvo(self)
        self.temperature = _Temperature(self)
        self.laser = _Laser(self)
        self.calibration = _Calibration(self)

    # ------------------------------------------------------------ plumbing
    def _request(self, method, path, json_body=None, timeout=None, **kw):
        url = f"{self.base_url}{path}"
        try:
            r = self._http.request(method, url, json=json_body,
                                   timeout=timeout or self.timeout, **kw)
        except requests.RequestException as exc:
            raise ScopioError(f"cannot reach the microscope at {url}: {exc}")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise ScopioError(f"{method} {path} -> {r.status_code}: {detail}",
                              status=r.status_code)
        try:
            return r.json()
        except ValueError:
            raise ScopioError(f"{method} {path} returned non-JSON")

    # ------------------------------------------------------ generic surface
    def call_service(self, path, body=None, timeout=None):
        """Call any ROS service (path relative to /scopio, e.g. 'stage/jog')."""
        q = f"?timeout={timeout}" if timeout else ""
        return self._request("POST", f"/api/v1/service/{path}{q}",
                             json_body=body or {},
                             timeout=(timeout or self.timeout) + 5.0)

    def subscribe(self, topic, callback, rate_hz=None):
        """Live-stream a topic. callback(msg_dict, envelope) runs on the SDK's
        WebSocket thread -- keep it quick. Returns a handle with .unsubscribe()."""
        return self._ws.subscribe(topic, callback, rate_hz=rate_hz)

    def send_goal(self, action, goal=None, on_feedback=None, timeout=600.0):
        """Run an action (autofocus, scans, stage paths) to completion."""
        return self._ws.send_goal(action, goal, on_feedback=on_feedback,
                                  timeout=timeout)

    def health(self):
        return self._request("GET", "/api/v1/health")

    def status(self):
        return self._request("GET", "/api/v1/status")

    def interfaces(self):
        return self._request("GET", "/api/v1/interfaces")

    def telemetry(self, topic):
        """Latest cached message of a state topic (from /api/v1/status)."""
        entry = self.status()["telemetry"].get(topic)
        return entry["msg"] if entry else None

    def stream_frames(self, chunk_size=16384, stall_timeout=15.0):
        """Generator of raw JPEG frames from the live camera stream.

        stall_timeout is a READ timeout, and it is the point: with no read
        timeout a stream that stops mid-flight (network drop, a proxy that
        stops forwarding, a camera that stops encoding) blocks this generator
        forever. The caller's reconnect loop never runs, and whatever last
        showed the frame keeps showing it -- video that looks frozen rather
        than disconnected. Generous by default: a long exposure legitimately
        means seconds between frames.
        """
        url = f"{self.base_url}/api/v1/stream.mjpg"
        try:
            r = self._http.get(url, stream=True,
                               timeout=(self.timeout, stall_timeout))
        except requests.RequestException as exc:
            raise ScopioError(f"cannot open camera stream: {exc}")
        if r.status_code >= 400:
            raise ScopioError(f"camera stream -> {r.status_code}",
                              status=r.status_code)
        return iter_jpegs(r, chunk_size=chunk_size)

    def close(self):
        self._ws.close()
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ------------------------------------------------------------- namespaces
class _Stage:
    def __init__(self, scope):
        self._s = scope

    def jog(self, dx=0, dy=0, dz=0):
        """Relative move in Sangaboard steps."""
        return self._s.call_service("stage/jog",
                                    {"dx": int(dx), "dy": int(dy), "dz": int(dz)})

    def move_abs(self, x, y, z):
        return self._s.call_service("stage/move_abs",
                                    {"x": int(x), "y": int(y), "z": int(z)})

    def position(self):
        return self._s.telemetry("stage/position")

    def move_path(self, points, settle_s=0.0, on_feedback=None, timeout=3600.0):
        """points: [{'x':..,'y':..,'z':..}, ...] visited in order."""
        return self._s.send_goal("stage/move_path",
                                 {"points": points, "settle_s": settle_s},
                                 on_feedback=on_feedback, timeout=timeout)

    def scan_region(self, x_min, x_max, y_min, y_max, step, settle_s=0.0,
                    on_feedback=None, timeout=3600.0):
        return self._s.send_goal("scan_region",
                                 {"x_min": x_min, "x_max": x_max,
                                  "y_min": y_min, "y_max": y_max,
                                  "step": step, "settle_s": settle_s},
                                 on_feedback=on_feedback, timeout=timeout)


class _Camera:
    """Camera controls go to the curated HTTP endpoints (they reach the camera
    server directly, so they work even while the ROS graph is rebuilding).
    Autofocus is the backend ROS action."""

    def __init__(self, scope):
        self._s = scope

    def get_controls(self):
        return self._s._request("GET", "/api/v1/camera/controls")

    def set_controls(self, **controls):
        """Partial update; only the fields you pass change. Fields: framerate,
        exposure, analogue_gain, red_gain, blue_gain, contrast, saturation,
        brightness, sharpness."""
        return self._s._request("POST", "/api/v1/camera/controls",
                                json_body=controls)

    def set_framerate(self, fps):
        return self.set_controls(framerate=float(fps))

    def set_mode(self, mode):
        """'detail' (full field of view, most pixels) or 'fast' (highest frame
        rate, cropped field). get_controls()['modes'] lists what this camera
        module offers, with each one's size and fps. Reconfiguring the sensor
        interrupts the video for a moment, and CHANGES um_per_px -- see
        Calibration.um_per_px_width."""
        return self._s._request("POST", "/api/v1/camera/mode",
                                json_body={"mode": mode},
                                timeout=self._s.timeout + 10.0)

    def white_balance(self):
        """One-shot AWB; blocks ~1.5 s while the camera converges."""
        return self._s._request("POST", "/api/v1/camera/white_balance",
                                timeout=self._s.timeout + 10.0)

    def focus_metric(self):
        return self._s._request("GET", "/api/v1/camera/focus")

    def state(self):
        return self._s.telemetry("camera/state")

    def autofocus(self, z_range=2000, steps=15, settle_s=0.2,
                  on_feedback=None, timeout=600.0):
        return self._s.send_goal("camera/autofocus",
                                 {"z_range": int(z_range), "steps": int(steps),
                                  "settle_s": float(settle_s)},
                                 on_feedback=on_feedback, timeout=timeout)


class _InstrumentCall:
    """Shared plumbing for the nodes that expose a whole driver class over one
    `InstrumentCall` service (galvo -> WaveGen, temperature -> TCLab)."""

    SERVICE = None      # e.g. "temperature/call"

    def __init__(self, scope):
        self._s = scope

    def call(self, method, *args, timeout=None, **kwargs):
        """Call any method of the instrument's driver class. Python args map
        straight through: call("apply_sine", 1000, 2.0, channel=2)."""
        resp = self._s.call_service(self.SERVICE, {
            "method": method,
            "args": json.dumps(list(args)) if args else "",
            "kwargs": json.dumps(kwargs) if kwargs else "",
        }, timeout=timeout)
        if not resp.get("success", False):
            raise ScopioError(f"{self.SERVICE} {method}: {resp.get('error')}",
                              payload=resp)
        try:
            return json.loads(resp.get("result") or "null")
        except ValueError:
            return resp.get("result")

    def methods(self):
        """Every method this instrument exposes: [{name, signature, doc}, ...].
        Answers even while the hardware is offline."""
        return self.call("list_methods")

    def connected(self):
        return bool(self.call("connected"))

    def reconnect(self):
        return bool(self.call("reconnect"))


class _Galvo(_InstrumentCall):
    SERVICE = "awg/call"

    def _scpi(self, service, command):
        resp = self._s.call_service(service, {"command": command})
        if not resp.get("success", False):
            raise ScopioError(f"{service} {command!r}: {resp.get('error')}",
                              payload=resp)
        return resp

    def write(self, command):
        """Send one raw SCPI command to the AWG. Raises on failure."""
        self._scpi("awg/write", command)

    def write_all(self, commands):
        """Send a list of SCPI commands in order (stops at the first failure)."""
        for cmd in commands:
            self.write(cmd)

    def query(self, command):
        """Send one SCPI query and return the instrument's reply string."""
        return self._scpi("awg/query", command)["response"]

    def status(self):
        return self._s.telemetry("awg/status")


class _Temperature(_InstrumentCall):
    """The TC LAB sample-temperature controller. `status()` is the cheap read
    (cached telemetry, no instrument traffic); everything else is a live call.

    Only the handful of things every app needs is sugared here -- PID, tuning,
    limits, sensor profiles and the rest are one .call() away, and .methods()
    lists them."""

    SERVICE = "temperature/call"

    def status(self):
        return self._s.telemetry("temperature/status")

    def temperature(self):
        """Control-sensor reading, live from the instrument."""
        return self.call("temperature")

    def setpoint(self, celsius=None):
        """Read the setpoint, or set it when `celsius` is given."""
        if celsius is None:
            return self.call("get_setpoint")
        return self.call("set_setpoint", float(celsius))

    def output(self, on=None):
        """Read or switch the TEC output (nothing heats/cools while it's off)."""
        if on is None:
            return self.call("output_enabled")
        return self.call("output", bool(on))


class _Laser:
    """The laser relay -- one GPIO pin on the Pi, on or off.

    is_on() reads cached telemetry (no traffic to the pin). It returns None when
    the microscope has never reported a state, which is NOT "off": treat an
    unknown laser as live."""

    def __init__(self, scope):
        self._s = scope

    def is_on(self):
        state = self._s.telemetry("relay/state")
        return None if state is None else bool(state.get("data", False))

    def set(self, on):
        return self._s.call_service("relay/set", {"data": bool(on)})

    def on(self):
        return self.set(True)

    def off(self):
        return self.set(False)


class _Calibration:
    FIELDS = ("um_per_px", "um_per_px_width",
              "steps_per_um_x", "steps_per_um_y", "steps_per_um_z")

    def __init__(self, scope):
        self._s = scope

    def get(self):
        return self._s.telemetry("calibration")

    def um_per_px(self, width=None):
        """The image scale for a frame `width` pixels wide, or None if uncalibrated.

        A scale is only meaningful next to the resolution it was measured at:
        switch the camera to another sensor mode and the same slide lands on a
        different number of pixels. Pass the width you actually have (from
        camera.get_controls()['width']) and this converts. Omit it to get the
        stored value unconverted.
        """
        cal = self.get() or {}
        if not cal.get("has_um_per_px"):
            return None
        scale = float(cal["um_per_px"])
        measured_at = int(cal.get("um_per_px_width") or 0)
        if width and measured_at:
            return scale * measured_at / float(width)
        return scale

    def set(self, **values):
        """Partial update -- unspecified fields are pre-filled with null
        (-> NaN -> 'leave unchanged') so nothing gets clobbered.

        Send um_per_px_width (the frame width you measured at) with any
        um_per_px, or the scale cannot be converted for another sensor mode."""
        body = {f: values.get(f) for f in self.FIELDS}
        unknown = set(values) - set(self.FIELDS)
        if unknown:
            raise ScopioError(f"unknown calibration fields: {sorted(unknown)}")
        # int field: no NaN sentinel, and 0 already means "leave unchanged".
        body["um_per_px_width"] = int(values.get("um_per_px_width") or 0)
        return self._s.call_service("calibration/set", body)
