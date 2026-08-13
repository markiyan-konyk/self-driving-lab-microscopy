"""Smoke test for the MCP server: every tool, against the mock gateway that
scopio_client's test already provides. No hardware, no Pi, no MCP client.

    python scopio_mcp/test_scopio_mcp.py        (or: pytest)

It proves the tools are registered, callable, and return what the docstrings
promise -- the failure mode this catches is an SDK change (or an mcp SDK
version bump) quietly breaking a tool nobody exercised.
"""

import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scopio_client"))
import test_scopio_client as mock  # noqa: E402

EXPECTED_TOOLS = {
    "describe_instrument", "status", "call_service", "send_goal", "stage_move",
    "camera_controls", "grab_frame", "white_balance", "focus_metric",
    "record_clip", "instrument_call", "temperature", "laser", "galvo_scpi",
    "camera_mode",
}


def _real_jpeg():
    buf = io.BytesIO()
    PILImage.new("RGB", (640, 480), (30, 60, 90)).save(buf, "JPEG")
    return buf.getvalue()


def test_tools():
    port = mock._free_port()
    mock._serve(port)
    mock.state["jpeg"] = _real_jpeg()

    os.environ["SCOPIO_URL"] = f"http://127.0.0.1:{port}"
    os.environ["SCOPIO_API_KEY"] = mock.KEY
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import server  # noqa: E402  (after the env is set -- that is the contract)

    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert registered == EXPECTED_TOOLS, f"tool set drifted: {registered}"

    # -- discovery: the INDEX is names only. The two driver classes are ~240
    #    methods between them; pulling those on every first call is what the
    #    tiering exists to avoid, so the index must not contain them.
    index = server.describe_instrument()
    assert index["services"] == ["stage/jog"], index["services"]
    assert set(index["instruments"]) == {"galvo", "temperature"}
    assert index["instruments"]["temperature"]["connected"] is False
    assert "signature" not in repr(index), "the index must not carry method dumps"

    # -- drilling in: a ROS interface by name, an instrument by name
    assert server.describe_instrument("stage/jog")["kind"] == "service"
    assert server.describe_instrument("/scopio/stage/jog")["name"] == "stage/jog"
    detail = server.describe_instrument("temperature")
    assert detail["methods"] == 36.6
    # The axis/channel mapping and the setpoint-vs-output split live HERE, not
    # only in the index: an agent that drills straight in must still see them.
    assert "CH1 = X" in server.describe_instrument("galvo")["what"]
    assert "output is a separate switch" in detail["what"].replace("TEC ", "")
    #    the mock's galvo is "offline": degrade to a message, never blow up
    assert "offline" in server.describe_instrument("galvo")["error"]
    try:
        server.describe_instrument("no/such/thing")
        raise AssertionError("an unknown subject must raise")
    except ValueError:
        pass

    assert server.status()["health"]["ok"] is True
    assert server.status()["telemetry"]["stage/position"]["msg"]["x"] == 1

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
    assert server.send_goal("camera/autofocus", {"steps": 3}) == {
        "status": "succeeded", "result": {"z": 7}}

    # -- motion, camera
    assert server.stage_move(dx=10)["echo"] == {"dx": 10, "dy": 0, "dz": 0}
    assert server.stage_move(1, 2, 3, absolute=True)["echo"] == {"x": 1, "y": 2, "z": 3}
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

    img = server.grab_frame(max_width=200)
    assert max(PILImage.open(io.BytesIO(img.data)).size) == 200

    # -- instruments: a reply string for a query, "ok" for a write
    assert server.galvo_scpi("*IDN?") == "RIGOL,DG1022Z"
    assert server.galvo_scpi(":OUTPut1 OFF") == "ok"
    assert server.instrument_call("temperature", "temperature") == 36.6
    try:
        server.instrument_call("nope", "x")
        raise AssertionError("an unknown instrument must raise")
    except ValueError:
        pass

    # -- recordings land under the CALLER's working directory, not next to the
    #    server, so the agent can read the frames back with its own file tools.
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            clip = server.record_clip(seconds=5, name="unit")
        finally:
            os.chdir(cwd)
        assert clip["dir"] == "recordings/unit"
        assert clip["frames"] == mock.N_FRAMES
        assert sorted(p.name for p in Path(clip["abs_dir"]).iterdir())[0] == "00000.jpg"
        assert Path(clip["abs_dir"]).is_relative_to(tmp)

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
    assert tools == EXPECTED_TOOLS, f"tool set drifted over stdio: {tools}"
    assert field(result, "is_error", "isError"), "an unreachable microscope " \
        "must surface as a tool error, not a crash"
    assert "cannot reach the microscope" in result.content[0].text
    print("registers over stdio")


if __name__ == "__main__":
    test_registers_over_stdio()
    test_tools()
