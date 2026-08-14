"""End-to-end check of scopio_client against a mock gateway. No hardware, no
Pi, no ROS -- it starts a FastAPI app that speaks the real API and drives the
SDK at it.

    python scopio_client/test_scopio_client.py        (or: pytest)

Covers the things that actually break: HTTP errors, the NaN-partial-update
convention, SCPI success/failure unwrapping, MJPEG frame splitting, stream
close, and the WebSocket subscribe/action/reconnect paths.
"""

import json
import socket
import threading
import time

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from scopio_client import Scopio, ScopioError
from scopio_client.client import convert_um_per_px
from scopio_client.stream import iter_jpegs

KEY = "testkey"
JPEG = b"\xff\xd8" + b"jpeg-payload" + b"\xff\xd9"
N_FRAMES = 5

app = FastAPI()
# "jpeg" is swappable so scopio_mcp's test can serve a real, decodable image.
state = {"drop_ws_after_message": False, "reject_ws": False,
         "controls": {"contrast": 1.0}, "jpeg": JPEG,
         "temperature_calls": [],
         # Sensor modes are reported separately from `controls` so the partial-
         # update assertions above stay exact.
         "camera_mode": {"mode": "detail", "width": 1640, "height": 1232,
                         "window": 3280, "framerate": 41.9,
                         "modes": {"detail": {"size": [1640, 1232], "fps": 41.9,
                                              "full_fov": True,
                                              "window": [3280, 2464]},
                                   "fast": {"size": [640, 480], "fps": 206.7,
                                            "full_fov": False,
                                            "window": [1280, 960]}}}}


def _auth(request):
    if request.headers.get("x-api-key") != KEY:
        raise HTTPException(401, "bad key")


# ------------------------------------------------------------------ mock HTTP
@app.get("/api/v1/health")
async def health():
    return {"ok": True, "camera_ok": True}


@app.get("/api/v1/status")
async def status(request: Request):
    _auth(request)
    return {"telemetry": {
        "stage/position": {"msg": {"x": 1, "y": 2, "z": 3}, "stamp": 0.0},
        "relay/state": {"msg": {"data": True}, "stamp": 0.0},
        "temperature/status": None,          # node not up yet -- must not crash
    }}


@app.get("/api/v1/interfaces")
async def interfaces(request: Request):
    _auth(request)
    return {"services": {"stage/jog": {}}, "topics": {}, "actions": {}}


@app.post("/api/v1/service/{path:path}")
async def service(path: str, request: Request, body: dict = Body(default={})):
    _auth(request)
    if path in ("stage/jog", "stage/move_abs"):
        return {"ok": True, "echo": body}
    if path == "calibration/set":
        return {"ok": True, "echo": body}
    if path == "relay/set":
        return {"success": True, "message": "Relay ON" if body["data"] else "Relay OFF"}
    if path == "awg/write":
        return {"success": True, "error": ""}
    if path == "awg/query":
        return {"success": True, "response": "RIGOL,DG1022Z", "error": ""}
    if path == "awg/call":
        return {"success": False, "result": "", "error": "instrument offline"}
    if path == "temperature/call":
        # Recorded so a caller can assert WHICH driver methods it drove, in
        # order -- a setpoint written without the output switched on heats
        # nothing, and that is invisible in the return value.
        state["temperature_calls"].append(
            (body.get("method"), json.loads(body.get("args") or "[]")))
        return {"success": True, "result": "36.6", "error": ""}
    raise HTTPException(404, f"No such service in the graph: {path}")


@app.get("/api/v1/camera/controls")
async def get_controls(request: Request):
    _auth(request)
    return {**state["controls"], **state["camera_mode"]}


@app.post("/api/v1/camera/controls")
async def set_controls(request: Request, body: dict = Body(default={})):
    _auth(request)
    state["controls"].update(body)           # partial update, like the real one
    return state["controls"]


@app.post("/api/v1/camera/mode")
async def camera_mode(request: Request, body: dict = Body(default={})):
    _auth(request)
    want = body.get("mode")
    if want not in state["camera_mode"]["modes"]:
        return {"error": f"mode must be one of {sorted(state['camera_mode']['modes'])}"}
    spec = state["camera_mode"]["modes"][want]
    state["camera_mode"].update(mode=want, width=spec["size"][0],
                                height=spec["size"][1], window=spec["window"][0],
                                framerate=spec["fps"])
    return state["camera_mode"]


@app.post("/api/v1/camera/white_balance")
async def white_balance(request: Request):
    _auth(request)
    return {"red_gain": 1.8, "blue_gain": 1.4}


@app.get("/api/v1/camera/focus")
async def focus(request: Request):
    _auth(request)
    return {"focus": 123.4}


@app.get("/api/v1/stream.mjpg")
async def stream(request: Request):
    _auth(request)

    def gen():
        jpeg = state["jpeg"]
        for _ in range(N_FRAMES):
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
            yield jpeg[:5]                   # split a frame across chunks
            yield jpeg[5:]
            yield b"\r\n"

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


# --------------------------------------------------------------- mock WebSocket
@app.websocket("/api/v1/ws")
async def ws(sock: WebSocket):
    if state["reject_ws"]:
        await sock.close()                   # refuse the upgrade -> client must retry
        return
    await sock.accept()
    if sock.query_params.get("api_key") != KEY:
        await sock.send_json({"op": "error", "id": None, "code": "unauthorized"})
        await sock.close()
        return
    try:
        while True:
            req = await sock.receive_json()
            op, mid = req.get("op"), req.get("id")
            if op == "subscribe":
                await sock.send_json({"op": "ok", "id": mid})
                await sock.send_json({"op": "message", "id": mid,
                                      "topic": req["topic"], "msg": {"x": 1}})
                if state["drop_ws_after_message"]:
                    state["drop_ws_after_message"] = False
                    await sock.close()
                    return
            elif op == "unsubscribe":
                await sock.send_json({"op": "ok", "id": mid})
            elif op == "action_send_goal":
                if req["action"] == "nope":
                    await sock.send_json({"op": "action_ack", "id": mid,
                                          "accepted": False})
                    continue
                await sock.send_json({"op": "action_ack", "id": mid, "accepted": True})
                await sock.send_json({"op": "action_feedback", "id": mid,
                                      "feedback": {"pct": 50}})
                await sock.send_json({"op": "action_result", "id": mid,
                                      "status": "succeeded", "result": {"z": 7}})
            else:
                await sock.send_json({"op": "error", "id": mid, "code": "bad_request",
                                      "detail": f"unknown op '{op}'"})
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------- harness
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.1)
    raise RuntimeError("mock gateway did not start")


def _wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ------------------------------------------------------------------------ tests
def test_offline_bits():
    """No server needed: JPEG framing and the stream-closing contract."""
    class FakeResponse:
        def __init__(self, chunks):
            self.chunks, self.closed = chunks, False

        def iter_content(self, chunk_size=None):
            return iter(self.chunks)

        def close(self):
            self.closed = True

    # Two frames arriving as awkwardly split chunks, with junk in between.
    r = FakeResponse([b"junk" + JPEG[:3], JPEG[3:] + b"--boundary--", JPEG])
    assert list(iter_jpegs(r)) == [JPEG, JPEG]
    assert r.closed, "iter_jpegs must close the response when it finishes"

    r = FakeResponse([JPEG, JPEG])
    gen = iter_jpegs(r)
    next(gen)
    gen.close()
    assert r.closed, "iter_jpegs must close the response when abandoned early"


def test_a_scale_survives_a_sensor_mode_change():
    """Cropping and binning move micrometres-per-pixel in different ways, and
    only one of them moves it at all. Scaling by image width alone -- which is
    the obvious thing to do and what this once did -- is wrong by 2.56x between
    a Camera Module 2's two modes, because BOTH are 2x binned: they differ in
    how much slide you see, not in how big a pixel is."""
    # detail: 1640 image px across a 3280 px sensor window (2x binned)
    # fast:    640 image px across a 1280 px sensor window (2x binned, cropped)
    assert convert_um_per_px(0.5, 1640, 3280, 640, 1280) == 0.5

    # An UNBINNED mode really does halve the scale: 1920 image px straight off
    # a 1920 px window is one sensor pixel each.
    assert convert_um_per_px(0.5, 1640, 3280, 1920, 1920) == 0.25
    # ...and back again.
    assert convert_um_per_px(0.25, 1920, 1920, 1640, 3280) == 0.5

    # A pure downscale with no crop doubles it, as the naive width rule expects.
    assert convert_um_per_px(0.5, 1640, 3280, 820, 3280) == 1.0

    # Nothing to convert from (a calibration taken before this existed, or an
    # unknown current mode) returns the stored value rather than a guess.
    for args in [(0, 3280, 640, 1280), (1640, 0, 640, 1280),
                 (1640, 3280, None, None), (1640, 3280, 640, 0)]:
        assert convert_um_per_px(0.5, *args) == 0.5


def test_against_mock_gateway():
    port = _free_port()
    _serve(port)

    # No scheme on purpose: typing "127.0.0.1:PORT" has to work.
    with Scopio(f"127.0.0.1:{port}", api_key=KEY, timeout=5.0) as scope:
        assert scope.base_url == f"http://127.0.0.1:{port}"

        # -- HTTP: plain calls, telemetry, errors
        assert scope.health()["ok"] is True
        assert scope.stage.position() == {"x": 1, "y": 2, "z": 3}
        assert scope.temperature.status() is None       # topic with no data yet
        assert scope.stage.jog(dx=10)["echo"] == {"dx": 10, "dy": 0, "dz": 0}

        try:
            scope.call_service("does/not/exist")
            raise AssertionError("a 404 service must raise")
        except ScopioError as exc:
            assert exc.status == 404 and "does/not/exist" in str(exc)

        # -- camera: partial update leaves the other fields alone
        scope.camera.set_controls(contrast=1.7)
        assert scope.camera.get_controls()["contrast"] == 1.7
        scope.camera.set_framerate(30)
        # The partial update is the point: framerate arrives, contrast survives.
        assert state["controls"] == {"contrast": 1.7, "framerate": 30.0}
        assert scope.camera.white_balance()["red_gain"] == 1.8

        # -- sensor mode: the two ways to run the camera, and a refusal
        assert scope.camera.set_mode("fast")["width"] == 640
        assert scope.camera.get_controls()["window"] == 1280
        assert "must be one of" in scope.camera.set_mode("4k")["error"]
        assert scope.camera.focus_metric() == {"focus": 123.4}

        # -- calibration: unnamed fields are sent as null (== "leave unchanged")
        echo = scope.calibration.set(um_per_px=0.42, um_per_px_width=1640,
                                     um_per_px_window=3280)["echo"]
        assert echo == {"um_per_px": 0.42, "um_per_px_width": 1640,
                        "um_per_px_window": 3280, "steps_per_um_x": None,
                        "steps_per_um_y": None, "steps_per_um_z": None}
        # An int field has no NaN, so "not given" has to travel as 0.
        assert scope.calibration.set(um_per_px=0.42)["echo"]["um_per_px_width"] == 0
        try:
            scope.calibration.set(nonsense=1)
            raise AssertionError("an unknown calibration field must raise")
        except ScopioError:
            pass

        # -- laser: is_on() reads telemetry, and "no state reported" is None
        # (unknown), NEVER False -- an unknown laser has to be treated as live.
        assert scope.laser.is_on() is True
        assert scope.laser.off()["message"] == "Relay OFF"
        assert scope.laser.on()["message"] == "Relay ON"

        # -- instruments: success unwraps to a value, failure raises
        assert scope.galvo.query("*IDN?") == "RIGOL,DG1022Z"
        scope.galvo.write(":OUTPut1 OFF")
        assert scope.temperature.temperature() == 36.6
        try:
            scope.galvo.methods()
            raise AssertionError("a failed instrument call must raise")
        except ScopioError as exc:
            assert "instrument offline" in str(exc)

        # -- MJPEG: whole frames out of arbitrary chunking
        frames = list(scope.stream_frames(chunk_size=7))
        assert frames == [JPEG] * N_FRAMES, f"got {len(frames)} frames"

        # -- WebSocket: actions, with feedback
        feedback = []
        result = scope.send_goal("camera/autofocus", {"steps": 3},
                                 on_feedback=feedback.append, timeout=10.0)
        assert result == {"status": "succeeded", "result": {"z": 7}}
        assert feedback == [{"pct": 50}]

        try:
            scope.send_goal("nope", timeout=10.0)
            raise AssertionError("a rejected goal must raise")
        except ScopioError as exc:
            assert "rejected" in str(exc)

        # -- WebSocket: subscribe, then survive a drop that the server refuses
        #    to let us straight back into. The recv loop has to keep retrying
        #    and re-send the subscription once the gateway is back.
        got = []
        state["drop_ws_after_message"] = True
        state["reject_ws"] = True
        sub = scope.subscribe("stage/position", lambda msg, env: got.append(msg))
        _wait_for(lambda: len(got) == 1, 5.0, "the first telemetry message")

        threading.Timer(1.5, lambda: state.update(reject_ws=False)).start()
        _wait_for(lambda: len(got) >= 2, 15.0,
                  "re-subscription after the gateway came back")
        sub.unsubscribe()

    print("all good")


if __name__ == "__main__":
    test_offline_bits()
    test_a_scale_survives_a_sensor_mode_change()
    test_against_mock_gateway()
