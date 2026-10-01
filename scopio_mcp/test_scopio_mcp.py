"""Smoke test for the MCP server: every tool, against the mock gateway that
scopio_client's test already provides. No hardware, no Pi, no MCP client.

    python scopio_mcp/test_scopio_mcp.py        (or: pytest)

It proves the tools are registered, callable, and return what the docstrings
promise -- the failure mode this catches is an SDK change (or an mcp SDK
version bump) quietly breaking a tool nobody exercised.
"""

import asyncio
import inspect
import io
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scopio_client"))
import test_scopio_client as mock  # noqa: E402

EXPECTED_TOOLS = {
    # discovery + generic surface
    "describe_instrument", "status", "call_service", "send_goal", "goal",
    "read_topic", "publish", "instrument_call",
    # motion
    "stage_move", "autofocus", "galvo_move", "galvo_scpi",
    # camera + scale
    "grab_frame", "record_clip", "camera_controls", "camera_mode",
    "white_balance", "focus_metric", "calibration",
    # instruments + time
    "temperature", "laser", "wait",
}


def run(tool, *args, **kwargs):
    """Call a tool the way the test needs, async or not."""
    result = tool(*args, **kwargs)
    return asyncio.run(result) if inspect.iscoroutine(result) else result


def _real_jpeg():
    buf = io.BytesIO()
    PILImage.new("RGB", (640, 480), (30, 60, 90)).save(buf, "JPEG")
    return buf.getvalue()


def _img(image):
    return PILImage.open(io.BytesIO(image.data))


def test_tools():
    port = mock._free_port()
    mock._serve(port)
    mock.state["jpeg"] = _real_jpeg()

    os.environ["SCOPIO_URL"] = f"http://127.0.0.1:{port}"
    os.environ["SCOPIO_API_KEY"] = mock.KEY
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import server  # noqa: E402  (after the env is set -- that is the contract)

    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert registered == EXPECTED_TOOLS, f"tool set drifted: {registered ^ EXPECTED_TOOLS}"

    # -- the brief carries the hardware rules no schema can express
    for rule in ("one axis", "2 v", "50 commands/s", "unknown", "measured fps"):
        assert rule in server.INSTRUCTIONS.lower(), f"instructions lost: {rule}"

    # -- discovery: the INDEX is names only, and instruments are DISCOVERED
    #    (every InstrumentCall service), under their agent-facing names.
    index = server.describe_instrument()
    assert "stage/jog" in index["services"], index["services"]
    assert set(index["instruments"]) == {"galvo", "temperature"}, index["instruments"]
    assert index["instruments"]["galvo"]["node"] == "awg"
    assert index["instruments"]["temperature"]["connected"] is False
    assert "signature" not in repr(index), "the index must not carry method dumps"

    # -- drilling in: a ROS interface by name, an instrument by either name
    assert server.describe_instrument("stage/jog")["kind"] == "service"
    assert server.describe_instrument("/scopio/stage/jog")["name"] == "stage/jog"
    assert server.describe_instrument("galvo")["methods"][0]["name"] == "update"
    assert server.describe_instrument("awg")["instrument"] == "awg"
    detail = server.describe_instrument("temperature")
    assert detail["methods"] == 36.6
    # The axis/channel mapping and the setpoint-vs-output split live HERE, not
    # only in the index: an agent that drills straight in must still see them.
    assert "CH1 = X" in server.describe_instrument("galvo")["what"]
    assert "output is a separate switch" in detail["what"].replace("TEC ", "")
    #    an offline instrument degrades to a message, never blows up
    mock.state["awg_offline"] = True
    try:
        assert "offline" in server.describe_instrument("galvo")["error"]
    finally:
        mock.state["awg_offline"] = False
    try:
        server.describe_instrument("no/such/thing")
        raise AssertionError("an unknown subject must raise")
    except ValueError:
        pass

    # -- status: everything, or one topic
    assert server.status()["health"]["ok"] is True
    assert server.status()["telemetry"]["stage/position"]["msg"]["x"] == 1
    assert server.status("stage/position")["entry"]["msg"]["x"] == 1

    # -- "set the sample to 20 C" must also DRIVE it, not just store a number
    assert server.temperature(celsius=20.0)["output_on"] is None  # mock: no telemetry
    assert mock.state["temperature_calls"][-2:] == [
        ("set_setpoint", [20.0]), ("output", [True])], mock.state["temperature_calls"]
    server.temperature(enable=False)
    assert mock.state["temperature_calls"][-1] == ("output", [False])

    # -- the laser: an unknown relay state is None (unknown), never False
    assert server.laser() == {"on": True}
    assert server.laser(on=False)["message"] == "Relay OFF"

    # -- generic surface
    assert server.call_service("stage/jog", {"dx": 5})["echo"] == {"dx": 5}
    assert run(server.send_goal, "camera/autofocus", {"steps": 3}) == {
        "status": "succeeded", "result": {"z": 7}}
    assert run(server.autofocus, steps=3) == {"status": "succeeded", "result": {"z": 7}}
    #    a background goal: returns at once, can be checked and canceled
    started = run(server.send_goal, "slow/scan", {"step": 10}, wait=False)
    assert run(server.goal, started["goal_id"])["done"] is False
    before = len(mock.state.get("canceled", []))
    run(server.goal, started["goal_id"], cancel=True)
    mock._wait_for(lambda: len(mock.state.get("canceled", [])) == before + 1, 5.0,
                   "the goal tool's cancel")
    assert server.read_topic("stage/position") == {
        "topic": "stage/position", "messages": [{"x": 1}], "complete": True}
    assert server.publish("/scopio/some/topic", {"data": 1}) == {"published": "some/topic"}
    assert mock.state["published"][-1] == ("some/topic", {"data": 1})
    assert run(server.wait, 0.05) == {"waited_s": 0.05}

    # -- galvo: sequenced, only the axes that change, longer across +/-2 V
    moved = server.galvo_move(x=1.0, y=2.5, axis_settle_s=0, range_settle_s=0)
    assert (moved["moved"], moved["range_switched"]) == (["x", "y"], ["y"]), moved

    # -- motion + camera
    assert server.stage_move(dx=10)["echo"] == {"dx": 10, "dy": 0, "dz": 0}
    assert server.stage_move(1, 2, 3, absolute=True)["echo"] == {"x": 1, "y": 2, "z": 3}
    moved, info, image = server.stage_move(dx=5, grab=True, max_width=100)
    assert moved["echo"]["dx"] == 5 and max(_img(image).size) == 100
    assert server.camera_controls({"contrast": 1.3})["contrast"] == 1.3
    assert server.camera_controls()["contrast"] == 1.3
    assert server.white_balance()["red_gain"] == 1.8

    # -- sensor mode: detail (full field of view) vs fast (cropped, high rate)
    assert server.camera_mode()["mode"] == "detail"
    assert server.camera_mode("fast")["width"] == 640
    try:
        server.camera_mode("4k")
        raise AssertionError("an unknown mode must raise")
    except ValueError:
        pass
    assert server.focus_metric() == {"focus": 123.4}

    # -- calibration: a scale is stored WITH the geometry it was measured on
    server.calibration(um_per_px=0.5)
    assert mock.state["calibration_set"]["um_per_px"] == 0.5
    assert mock.state["calibration_set"]["um_per_px_width"] == 640   # 'fast' is running
    assert server.calibration()["frame"] == [640, 480]

    # -- grab_frame: geometry first, then the picture; roi is a digital zoom
    info, image = server.grab_frame(max_width=200)
    assert info["frame"] == [640, 480] and max(_img(image).size) == 200
    info, image = server.grab_frame(roi=[10, 20, 100, 50])
    assert info["roi"] == [10, 20, 100, 50] and _img(image).size == (100, 50)
    #    ...and through the real MCP conversion: an image block plus text
    content = asyncio.run(server.mcp.call_tool("grab_frame", {"max_width": 64}))
    content = content[0] if isinstance(content, tuple) else content
    assert sorted(c.type for c in content) == ["image", "text"], content

    # -- instruments: a reply string for a query, "ok" for a write
    assert server.galvo_scpi("*IDN?") == "RIGOL,DG1022Z"
    assert server.galvo_scpi(":OUTPut1 OFF") == "ok"
    assert server.instrument_call("temperature", "temperature") == 36.6
    assert server.instrument_call("galvo", "position") == {"x": 1.0, "y": 2.5}
    try:
        server.instrument_call("nope", "x")
        raise AssertionError("an unknown instrument must raise")
    except ValueError as exc:
        assert "galvo" in str(exc), "the error must list what does exist"

    # -- files land under the CALLER's working directory, not next to the
    #    server, so the agent can read them back with its own file tools --
    #    and an agent-chosen name cannot climb out of recordings/.
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            clip = server.record_clip(seconds=5, name="../../unit")
            info, _ = server.grab_frame(save_as="still")
        finally:
            os.chdir(cwd)
        assert clip["dir"] == "recordings/unit"
        assert clip["frames"] == mock.N_FRAMES
        assert sorted(p.name for p in Path(clip["abs_dir"]).iterdir())[0] == "00000.jpg"
        assert Path(clip["abs_dir"]).is_relative_to(tmp)
        assert info["saved"] == "recordings/frames/still.jpg"
        assert (Path(tmp) / info["saved"]).read_bytes() == mock.state["jpeg"]

    print("all good")


def test_registers_over_stdio():
    """The thing every other test assumes: an MCP client can START this server
    and list its tools. Deliberately with NO microscope reachable -- registering
    the server must never depend on the Pi being up, or a rig that is switched
    off looks like a broken install.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def handshake():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parent / "server.py")],
            env=dict(os.environ, SCOPIO_URL="http://127.0.0.1:1",
                     SCOPIO_API_KEY="unused"))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                info = await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                # An unreachable microscope is a tool ERROR, never a crash.
                result = await session.call_tool("status", {})
                return info, tools, result

    # mcp 2.0 renamed the camelCase result fields; this test covers both SDKs.
    def field(obj, *names):
        return next(getattr(obj, n) for n in names if hasattr(obj, n))

    info, tools, result = asyncio.run(handshake())
    assert field(info, "server_info", "serverInfo").name == "scopio", info
    assert "INSTRUMENT RULES" in (info.instructions or ""), "the brief must reach the agent"
    assert tools == EXPECTED_TOOLS, f"tool set drifted over stdio: {tools ^ EXPECTED_TOOLS}"
    assert field(result, "is_error", "isError"), "an unreachable microscope " \
        "must surface as a tool error, not a crash"
    assert "cannot reach the microscope" in result.content[0].text
    print("registers over stdio")


if __name__ == "__main__":
    test_registers_over_stdio()
    test_tools()
