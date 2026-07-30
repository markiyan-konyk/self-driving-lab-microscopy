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
    "record_clip", "instrument_call", "galvo_scpi",
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

    # -- discovery. The mock's galvo is "offline", so describe_instrument must
    #    degrade to a message instead of blowing up.
    desc = server.describe_instrument()
    assert "services" in desc
    assert "unavailable" in desc["instrument_methods"]["galvo"]
    assert desc["instrument_methods"]["temperature"] == 36.6

    assert server.status()["health"]["ok"] is True
    assert server.status()["telemetry"]["stage/position"]["msg"]["x"] == 1

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


if __name__ == "__main__":
    test_tools()
