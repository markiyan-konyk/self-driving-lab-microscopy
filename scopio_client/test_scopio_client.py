"""End-to-end check of scopio_client against a mock gateway. No hardware, no
Pi, no ROS -- it starts a FastAPI app that speaks the real API and drives the
SDK at it.

    python scopio_client/test_scopio_client.py        (or: pytest)

Covers the things that actually break: HTTP errors, the NaN-partial-update
convention, SCPI success/failure unwrapping, MJPEG frame splitting, stream
close, and the WebSocket subscribe/action/reconnect paths.
"""

import socket
import threading
import time

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from scopio_client import Scopio, ScopioError
from scopio_client.stream import iter_jpegs

KEY = "testkey"
JPEG = b"\xff\xd8" + b"jpeg-payload" + b"\xff\xd9"
N_FRAMES = 5

app = FastAPI()
# "jpeg" is swappable so scopio_mcp's test can serve a real, decodable image.
state = {"drop_ws_after_message": False, "reject_ws": False,
         "controls": {"contrast": 1.0}, "jpeg": JPEG}


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
    if path == "awg/write":
        return {"success": True, "error": ""}
    if path == "awg/query":
        return {"success": True, "response": "RIGOL,DG1022Z", "error": ""}
    if path == "awg/call":
        return {"success": False, "result": "", "error": "instrument offline"}
    if path == "temperature/call":
        return {"success": True, "result": "36.6", "error": ""}
    raise HTTPException(404, f"No such service in the graph: {path}")


@app.get("/api/v1/camera/controls")
async def get_controls(request: Request):
    _auth(request)
    return state["controls"]


@app.post("/api/v1/camera/controls")
async def set_controls(request: Request, body: dict = Body(default={})):
    _auth(request)
    state["controls"].update(body)           # partial update, like the real one
    return state["controls"]


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
        assert scope.camera.get_controls() == {"contrast": 1.7}
        scope.camera.set_framerate(30)
        assert scope.camera.get_controls() == {"contrast": 1.7, "framerate": 30.0}
        assert scope.camera.white_balance()["red_gain"] == 1.8
        assert scope.camera.focus_metric() == {"focus": 123.4}

        # -- calibration: unnamed fields are sent as null (== "leave unchanged")
        echo = scope.calibration.set(um_per_px=0.42)["echo"]
        assert echo == {"um_per_px": 0.42, "steps_per_um_x": None,
                        "steps_per_um_y": None, "steps_per_um_z": None}
        try:
            scope.calibration.set(nonsense=1)
            raise AssertionError("an unknown calibration field must raise")
        except ScopioError:
            pass

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
    test_against_mock_gateway()
