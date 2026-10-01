"""The Scopio client: one object per microscope.

    scope = Scopio("http://10.42.0.1:8000", api_key="...")

Generic surface (works for every current AND future capability):
    scope.call_service("stage/jog", {"dx": 100})     # any ROS service, JSON in/out
    scope.subscribe("stage/position", cb)            # any (small) topic, live
    scope.publish("some/topic", {...})               # publish on a topic
    scope.send_goal("camera/autofocus", {...})       # any action, blocking
    scope.start_goal("scan_region", {...})           # any action, non-blocking
    scope.interfaces()                               # ask the microscope what it has
    scope.instruments()                              # every instrument node, by name
    scope.instrument("awg").call("sine", 2, freq=5)  # any instrument's driver

Convenience namespaces (thin sugar over the generic surface):
    scope.stage.jog(dx=..., dy=..., dz=...) / move_abs(x, y, z) / position()
                / move_path(points) / scan_region(...)
    scope.camera.get_controls() / set_controls(...) / set_framerate(fps)
                 / set_mode(m) / white_balance() / autofocus(...) / snapshot()
    scope.galvo.move_xy(x, y) / call(...) / write(cmd) / query(cmd) / status()
    scope.temperature.temperature() / setpoint(c) / output(on) / status()
    scope.laser.on() / off() / set(bool) / is_on()
    scope.calibration.get() / set(um_per_px=...) / um_per_px_now()
    scope.stream_frames()                            # generator of JPEG bytes

Instrument classes: the galvo and temperature nodes each own a driver CLASS and
expose every one of its methods over one service. Reach anything the sugar
above doesn't cover with .call(), and ask the instrument what it has:
    scope.temperature.call("set_pid", 1.2, i=0.4)
    scope.galvo.call("sine", 2, freq=1000, amp=2.0)
    [m["name"] for m in scope.temperature.methods()]

Both instruments are PACED by their drivers on the Pi (under 60 commands/s --
their input buffers are tiny). Calling faster than that does not fail; each
call just waits its turn, so a tight loop runs at the instrument's pace.

FAILURES RAISE. Every call either returns what it promises or raises
ScopioError -- including a service that answered success=false (a stage that is
unplugged, a relay that refused), which must never come back looking like a
result.

NaN sentinels: calibration/set (and the ROS camera/set_controls service) treat
NaN as "leave unchanged". The convenience methods pre-fill JSON null (-> NaN)
for every field you don't pass, so partial updates are safe by default.
"""

import json
import time
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from .errors import ScopioError
from .stream import iter_jpegs
from .ws import WsManager

DEFAULT_TIMEOUT = 15.0
# Extra attempts after a failed CONNECTION -- only where repeating cannot
# double an action: any GET, and a connect that timed out (the request never
# left). A jog whose reply was lost is never re-sent.
RETRIES = 2
RETRY_BACKOFF_S = 0.5


def _checked(resp, what):
    """A service that answered success=false is a failure, not a result."""
    if isinstance(resp, dict) and resp.get("success") is False:
        raise ScopioError(f"{what}: {resp.get('message') or resp.get('error') or 'failed'}",
                          payload=resp)
    return resp


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
        # One client is shared by every thread of an app (the UI runs a dozen);
        # the default pool of 10 then drops and re-opens connections under load.
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=32)
        self._http.mount("http://", adapter)
        self._http.mount("https://", adapter)
        # http -> ws, https -> wss.
        ws_url = ("ws" + self.base_url[4:] +
                  f"/api/v1/ws?api_key={quote(api_key, safe='')}")
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
        for attempt in range(RETRIES + 1):
            try:
                r = self._http.request(method, url, json=json_body,
                                       timeout=timeout or self.timeout, **kw)
                break
            except requests.ConnectionError as exc:
                safe = method == "GET" or isinstance(exc, requests.ConnectTimeout)
                if not safe or attempt == RETRIES:
                    raise ScopioError(f"cannot reach the microscope at {url}: {exc}")
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
            except requests.RequestException as exc:
                raise ScopioError(f"cannot reach the microscope at {url}: {exc}")
        if r.status_code >= 400:
            try:
                body = r.json()
                # FastAPI errors are {"detail": ...}; anything else (a proxy's
                # JSON list, a bare string) must still produce a ScopioError,
                # not an AttributeError out of the error path itself.
                detail = body.get("detail", body) if isinstance(body, dict) else body
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
        """Call any ROS service (path relative to /scopio, e.g. 'stage/jog').
        Returns the response fields as a dict, exactly as the node sent them."""
        q = f"?timeout={timeout}" if timeout else ""
        return self._request("POST", f"/api/v1/service/{path}{q}",
                             json_body=body or {},
                             timeout=(timeout or self.timeout) + 5.0)

    def subscribe(self, topic, callback, rate_hz=None):
        """Live-stream a topic. callback(msg_dict, envelope) runs on the SDK's
        WebSocket thread -- keep it quick. Survives reconnects. Returns a handle
        with .unsubscribe()."""
        return self._ws.subscribe(topic, callback, rate_hz=rate_hz)

    def publish(self, topic, msg, msg_type=None):
        """Publish one message on a topic. msg_type ('pkg/msg/Type') is needed
        only when nothing publishes on that topic yet."""
        self._ws.publish(topic, msg, msg_type)

    def send_goal(self, action, goal=None, on_feedback=None, timeout=600.0):
        """Run an action (autofocus, scans, stage paths) to completion. Returns
        {"status", "result"}; a timeout or Ctrl-C CANCELS the goal first."""
        return self._ws.send_goal(action, goal, on_feedback=on_feedback,
                                  timeout=timeout)

    def start_goal(self, action, goal=None, on_feedback=None):
        """Start an action and return at once with a Goal handle:
        .feedback (latest), .done(), .wait(timeout), .cancel()."""
        return self._ws.start_goal(action, goal, on_feedback=on_feedback)

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

    def instruments(self):
        """{name: service} for every instrument node -- every service of type
        InstrumentCall, e.g. {"awg": "awg/call", "temperature": ...}. A new
        instrument node appears here with no SDK change."""
        out = {}
        for full, spec in self.interfaces().get("services", {}).items():
            if not str((spec or {}).get("type", "")).endswith("/InstrumentCall"):
                continue
            rel = full[len("/scopio/"):] if full.startswith("/scopio/") else full.lstrip("/")
            if rel.endswith("/call"):
                out[rel[:-len("/call")]] = rel
        return out

    def instrument(self, name):
        """The driver of any instrument node, by name ("awg", "temperature", or
        a node added later): .call(method, ...), .methods(), .reconnect()."""
        known = {"awg": self.galvo, "galvo": self.galvo, "temperature": self.temperature}
        return known.get(name) or _InstrumentCall(self, service=f"{name}/call")

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
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = None
            r.close()          # a streamed error response still holds a connection
            raise ScopioError(f"camera stream -> {r.status_code}"
                              + (f": {detail}" if detail else ""),
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
    """The Sangaboard stage, in STEPS. Position is open-loop: counted from where
    the stage was when the stage node started, and lost if it restarts."""

    def __init__(self, scope):
        self._s = scope

    def jog(self, dx=0, dy=0, dz=0):
        """Relative move in steps; returns once the stage has stopped, with the
        new position (x, y, z). Raises if the board refused."""
        return _checked(self._s.call_service(
            "stage/jog", {"dx": int(dx), "dy": int(dy), "dz": int(dz)}), "stage/jog")

    def move_abs(self, x, y, z):
        """Absolute move in steps; returns once stopped, with the position."""
        return _checked(self._s.call_service(
            "stage/move_abs", {"x": int(x), "y": int(y), "z": int(z)}), "stage/move_abs")

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

    def snapshot(self, discard=0):
        """One fresh JPEG frame (bytes). The stream only ever delivers frames
        encoded AFTER it opens; discard=N skips N more, e.g. to let a frame
        exposed during a stage move go by."""
        frames = self._s.stream_frames()
        try:
            for i, jpeg in enumerate(frames):
                if i >= discard:
                    return jpeg
        finally:
            frames.close()
        raise ScopioError("camera stream ended before a frame arrived")

    def autofocus(self, z_range=2000, steps=15, settle_s=0.2,
                  on_feedback=None, timeout=600.0):
        return self._s.send_goal("camera/autofocus",
                                 {"z_range": int(z_range), "steps": int(steps),
                                  "settle_s": float(settle_s)},
                                 on_feedback=on_feedback, timeout=timeout)


class _InstrumentCall:
    """Shared plumbing for the nodes that expose a whole driver class over one
    `InstrumentCall` service (galvo -> DG1022Z, temperature -> TC10LAB, and any
    instrument node added later -- see Scopio.instrument)."""

    SERVICE = None      # e.g. "temperature/call"

    def __init__(self, scope, service=None):
        self._s = scope
        if service:
            self.SERVICE = service

    def call(self, method, *args, timeout=None, **kwargs):
        """Call any method of the instrument's driver class. Python args map
        straight through: call("sine", 2, freq=1000, amp=2.0)."""
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

    def status(self):
        """This instrument's cached status topic (<name>/status), or None."""
        return self._s.telemetry(self.SERVICE.rsplit("/", 1)[0] + "/status")


class _Galvo(_InstrumentCall):
    """The Rigol DG1022Z steering the tweezers' galvo mirrors: CH1 = X, CH2 = Y.
    Positions are DEFLECTIONS in volts from the calibrated centre (offsets()).

    The AWG's physics that move_xy() respects -- write your own sequences the
    same way:
      * switching which CHANNEL is being commanded moves something mechanical
        inside the box: move one axis, wait, then the other;
      * |output| <= RANGE_V at the connector is one output range; leaving or
        entering it flips a relay, which is slower still (and wears it).
    """

    SERVICE = "awg/call"
    # Settle times: conservative starting points -- tune them on the rig.
    AXIS_SETTLE_S = 0.2       # after each single-axis move
    RANGE_V = 2.0             # connector volts; one output range inside +/- this
    RANGE_SETTLE_S = 0.5      # after a move that crossed +/- RANGE_V

    def move_xy(self, x=None, y=None, axis_settle_s=None, range_settle_s=None):
        """Move the mirrors by DC offset the way the AWG needs it: ONE axis at a
        time, only the axes that actually change (an unchanged axis is not
        re-sent, so it costs no channel switch), waiting after each move --
        longer when the move crossed the +/-RANGE_V range boundary at the
        connector (deflection + offset). Returns once settled:
        {"x", "y", "moved": [...], "range_switched": [...], "waited_s"}."""
        axis_wait = self.AXIS_SETTLE_S if axis_settle_s is None else float(axis_settle_s)
        range_wait = self.RANGE_SETTLE_S if range_settle_s is None else float(range_settle_s)
        pos = self.call("position")
        offsets = self.call("offsets")
        moved, switched, waited = [], [], 0.0
        for axis, channel, target in (("x", 1, x), ("y", 2, y)):
            if target is None:
                continue
            target = float(target)
            if abs(target - float(pos[axis])) < 1e-6:
                continue
            before = float(pos[axis]) + float(offsets[axis])
            after = target + float(offsets[axis])
            crossed = (abs(before) <= self.RANGE_V) != (abs(after) <= self.RANGE_V)
            pos = self.call("update", channel, target)
            wait = range_wait if crossed else axis_wait
            time.sleep(wait)
            waited += wait
            moved.append(axis)
            if crossed:
                switched.append(axis)
        return {"x": pos["x"], "y": pos["y"], "moved": moved,
                "range_switched": switched, "waited_s": round(waited, 3)}

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


class _Temperature(_InstrumentCall):
    """The TC LAB sample-temperature controller. `status()` is the cheap read
    (cached telemetry, no instrument traffic); everything else is a live call.

    Only the handful of things every app needs is sugared here -- PID, tuning,
    limits, sensor profiles and the rest are one .call() away, and .methods()
    lists them."""

    SERVICE = "temperature/call"

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
    unknown laser as live. set()/on()/off() RAISE if the relay refused -- a
    failed "off" must never pass for a successful one."""

    def __init__(self, scope):
        self._s = scope

    def is_on(self):
        state = self._s.telemetry("relay/state")
        return None if state is None else bool(state.get("data", False))

    def set(self, on):
        return _checked(self._s.call_service("relay/set", {"data": bool(on)}),
                        f"laser relay {'ON' if on else 'OFF'}")

    def on(self):
        return self.set(True)

    def off(self):
        return self.set(False)


def convert_um_per_px(um_per_px, measured_width, measured_window, width, window):
    """Carry a scale from the sensor mode it was measured in to another one.

    THE reference implementation of the conversion in Calibration.msg. What the
    scale tracks is SENSOR PIXELS PER IMAGE PIXEL -- window / width. A mode can
    change that two independent ways and only one of them moves the scale:

      binning   two sensor pixels into one image pixel     -> scale doubles
      cropping  reads a smaller window into the same image -> scale UNCHANGED,
                                                              you just see less

    Scaling by image width alone conflates them, and on a Camera Module 2 that
    is wrong by 2.56x between the two modes -- both are 2x binned, so their
    micrometres per pixel are identical while their widths are not.

    Any missing/zero argument means "no basis to convert", and the scale is
    returned untouched rather than guessed at.
    """
    have = all(v and v > 0 for v in (measured_width, measured_window, width, window))
    if not have:
        return um_per_px
    return um_per_px * (window / float(width)) / (measured_window / float(measured_width))


class _Calibration:
    FIELDS = ("um_per_px", "um_per_px_width", "um_per_px_window",
              "steps_per_um_x", "steps_per_um_y", "steps_per_um_z")

    def __init__(self, scope):
        self._s = scope

    def get(self):
        return self._s.telemetry("calibration")

    def um_per_px(self, width=None, window=None):
        """The image scale for the frame you actually have, or None if uncalibrated.

        Pass width and window from camera.get_controls() ('width' and 'window')
        and the stored scale is converted from the mode it was measured in.
        Omit them to get the stored value unconverted.
        """
        cal = self.get() or {}
        if not cal.get("has_um_per_px"):
            return None
        return convert_um_per_px(float(cal["um_per_px"]),
                                 int(cal.get("um_per_px_width") or 0),
                                 int(cal.get("um_per_px_window") or 0),
                                 width, window)

    def um_per_px_now(self):
        """The image scale for the sensor mode running RIGHT NOW (one extra
        request to read it), or None if uncalibrated."""
        controls = self._s.camera.get_controls()
        return self.um_per_px(controls.get("width"), controls.get("window"))

    def set(self, **values):
        """Partial update -- unspecified fields are pre-filled with null
        (-> NaN -> 'leave unchanged') so nothing gets clobbered.

        Send um_per_px_width AND um_per_px_window (the frame width, and the
        sensor window width behind it) with any um_per_px, or the scale cannot
        be converted for another sensor mode."""
        body = {f: values.get(f) for f in self.FIELDS}
        unknown = set(values) - set(self.FIELDS)
        if unknown:
            raise ScopioError(f"unknown calibration fields: {sorted(unknown)}")
        # int fields: no NaN sentinel, and 0 already means "leave unchanged".
        for f in ("um_per_px_width", "um_per_px_window"):
            body[f] = int(values.get(f) or 0)
        return _checked(self._s.call_service("calibration/set", body), "calibration/set")
