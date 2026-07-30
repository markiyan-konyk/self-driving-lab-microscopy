#!/usr/bin/env python3
"""Offline check of the logic that has no hardware in it.

    python3 scripts/test_drivers.py        # exits non-zero on the first failure

No ROS, no pyvisa session, no instruments -- it drives the drivers against a
fake device that records the SCPI it is given. It exists to catch the class of
bug that used to hide in here: a ramp that silently did nothing, a sentinel that
resolved to an invalid amplitude, a partial request that clobbered every field
it did not mention.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src", "scopio_microscope"))

# The drivers import pyvisa at module scope; stub it so this runs anywhere.
sys.modules.setdefault("pyvisa", types.ModuleType("pyvisa"))

from scopio_microscope.drivers import dispatch                      # noqa: E402
from scopio_microscope.drivers.TC10LAB import (CONDITION_BITS, FAULT_BITS,  # noqa: E402
                                               TC10LAB)
from scopio_microscope.drivers.dg1022z import DG1022Z, usb_vid      # noqa: E402


class FakeDevice:
    """Records writes; answers queries from a canned table."""

    def __init__(self, replies=None):
        self.writes = []
        self.replies = replies or {}

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        return self.replies.get(cmd, "0")

    def close(self):
        pass


def awg(**replies):
    gen = DG1022Z("USB0::0x1AB1::0x0642::FAKE::INSTR")
    gen.device = FakeDevice(replies)
    return gen


def offsets(writes, prefix):
    """The offset values written for one channel, in order."""
    return [float(w.rsplit(" ", 1)[1]) for w in writes if w.startswith(prefix)]


# --------------------------------------------------------------- resources
def test_usb_vid():
    assert usb_vid("USB0::0x1AB1::0x0642::DG1ZA::INSTR") == 0x1AB1
    assert usb_vid("USB0::6833::1602::DG1ZA::INSTR") == 6833   # pyvisa-py decimal
    assert usb_vid("TCPIP::192.168.1.50::INSTR") is None
    assert usb_vid("ASRL/dev/ttyACM0::INSTR") is None
    assert usb_vid("USB0") is None                             # too few fields


# ------------------------------------------------------------- galvo moves
def test_update_applies_offset_and_validates_channel():
    gen = awg()
    gen.offsets(x=0.25, y=-0.10)
    gen.update(1, 1.0)
    gen.update(2, 1.0)
    assert gen.position() == {"x": 1.0, "y": 1.0}
    assert gen.device.writes == [":SOURce1:VOLTage:OFFSet 1.250",
                                 ":SOURce2:VOLTage:OFFSet 0.900"]
    for bad in (0, 3, -1):
        try:
            gen.update(bad, 1.0)
        except ValueError:
            continue
        raise AssertionError(f"channel {bad} should be refused")


def test_move_ramps_in_both_directions():
    """The old if/if/else meant an UPWARD move returned without doing anything."""
    for target in (2.0, -2.0):
        gen = awg()
        gen.move(1, target, t=0.0, steps=8)
        written = offsets(gen.device.writes, ":SOURce1")
        assert len(written) == 8, f"{target}: {len(written)} steps, expected 8"
        assert written == sorted(written, reverse=target < 0), \
            f"{target}: not monotonic: {written}"
        assert abs(written[-1] - target) < 1e-9, f"{target}: ended at {written[-1]}"
        assert gen.xpos == target


def test_move_honours_the_offset_and_starting_position():
    gen = awg()
    gen.offsets(x=0.5)
    gen.update(1, 1.0)
    gen.device.writes.clear()
    gen.move(1, 2.0, t=0.0, steps=4)
    # From xpos=1.0 to 2.0 in 4 steps, each written on top of the 0.5 offset.
    assert offsets(gen.device.writes, ":SOURce1") == [1.75, 2.0, 2.25, 2.5]


def test_dcinit_zeroes_the_remembered_position():
    """dcinit writes the offsets, so the position it leaves behind IS zero --
    otherwise the next update() jumps by a stale amount."""
    gen = awg()
    gen.offsets(x=0.3, y=0.4)
    gen.update(1, 1.5)
    gen.dcinit()
    assert gen.position() == {"x": 0.0, "y": 0.0}
    assert ":SOURce1:APPLy:DC 1,1,0.300" in gen.device.writes
    assert ":OUTP1 ON;:OUTP2 ON" in gen.device.writes


def test_sin_sentinels_never_command_zero_amplitude():
    gen = awg()
    try:
        gen.sininit()                    # nothing remembered yet
    except ValueError:
        pass
    else:
        raise AssertionError("sininit with amp 0 must refuse, not emit 0 Vpp SCPI")
    gen.sininit(freq=50, amp=2.0)
    assert gen.device.writes == [":SOURce1:APPLy:SINusoid 50.0,2.0,0.000,0.0",
                                 ":SOURce2:APPLy:SINusoid 50.0,2.0,0.000,0.0"]
    gen.device.writes.clear()
    gen.sinupdate(1, freq=120)           # amp/phase must be REMEMBERED, not zeroed
    assert gen.device.writes == [":SOURce1:FREQuency 120.0",
                                 ":SOURce1:PHASe 0.0",
                                 ":SOURce1:VOLTage 2.0"]
    assert (gen.freq, gen.amp) == (120.0, 2.0)


def test_every_scpi_call_goes_through_the_lock():
    """A method that touches self.device directly would bypass _lock and let two
    service calls interleave on one USB-TMC session."""
    import inspect

    from scopio_microscope.drivers import dg1022z
    source = inspect.getsource(dg1022z)
    allowed = ("self.device.read_termination", "self.device.write_termination",
               "self.device.timeout", "self.device.close()",
               "self.device.write(cmd)", "self.device.query(cmd)")
    for lineno, line in enumerate(source.splitlines(), 1):
        if "self.device." in line and not any(a in line for a in allowed):
            raise AssertionError(f"dg1022z.py:{lineno} bypasses command()/query(): "
                                 f"{line.strip()}")


def test_closed_session_is_a_clear_error():
    gen = awg()
    gen._drop()
    for call in (lambda: gen.command(":OUTP1 ON"), lambda: gen.query("*IDN?")):
        try:
            call()
        except ConnectionError:
            continue
        raise AssertionError("a closed session must raise, not AttributeError")
    gen._drop()          # idempotent


# --------------------------------------------------------------- TC10 LAB
def test_condition_bits_decode():
    tc = TC10LAB.__new__(TC10LAB)          # no session needed for pure decoding
    for bit in FAULT_BITS:
        assert bit in CONDITION_BITS, f"FAULT_BITS names bit {bit} with no label"
    cond = (1 << 6) | (1 << 10) | (1 << 9)      # sensor_open + output_on + in_tol
    tc.query_int = lambda cmd: cond
    tc.query_float = lambda cmd: 25.0
    tc.units = "C"
    s = tc.status()
    assert s["output"] is True and s["in_tolerance"] is True
    assert s["faults"] == ["sensor_open"], s["faults"]
    assert s["units"] == "C"


def test_status_costs_five_round_trips():
    """Every extra query is another chance per second for the instrument to be
    mid-reply when the next one arrives."""
    tc = TC10LAB.__new__(TC10LAB)
    asked = []
    tc.query_int = lambda cmd: asked.append(cmd) or 0
    tc.query_float = lambda cmd: asked.append(cmd) or 0.0
    tc.units = "C"
    tc.status()
    assert len(asked) == 5, asked
    assert "TEC:UNITS?" not in asked      # cached by set_units()/get_units()


def test_dev_path_uses_the_kernel_usbtmc_transport():
    """TCLAB_RESOURCE=/dev/usbtmc0 must not be handed to pyvisa (which cannot
    parse it) -- that silently looked like 'the instrument is not there'."""
    tc = TC10LAB("/dev/usbtmc-does-not-exist")
    try:
        tc._open()
    except OSError:
        pass                              # reached the char device, as intended
    else:
        raise AssertionError("expected an OSError from the missing device node")
    assert tc.rm is None, "pyvisa must not be involved for a /dev path"


# ---------------------------------------------------------------- dispatch
def test_dispatch_arguments():
    gen = awg()
    assert dispatch.call(gen, "position") == '{"x": 0.0, "y": 0.0}'
    dispatch.call(gen, "update", "[1, 0.5]")
    assert gen.xpos == 0.5
    dispatch.call(gen, "update", "", '{"ch": 2, "val": 0.25}')
    assert gen.ypos == 0.25
    dispatch.call(gen, "offsets", "0.75")          # bare scalar -> one positional
    assert gen.xoffset == 0.75

    for method, args, kwargs in [("_open", "", ""),          # private
                                 ("nope", "", ""),           # unknown
                                 ("update", "[1]", ""),      # wrong arity
                                 ("update", "[1,", ""),      # bad JSON
                                 ("update", "", "[1]"),      # kwargs not an object
                                 ("xpos", "", "")]:          # attribute, not method
        try:
            dispatch.call(gen, method, args, kwargs)
        except dispatch.DispatchError:
            continue
        raise AssertionError(f"{method}({args}, {kwargs}) should be a DispatchError")


def test_dispatch_json_is_strict_parser_safe():
    assert dispatch.to_json(float("nan")) == "null"
    assert dispatch.to_json({"a": [float("inf"), 1.5]}) == '{"a": [null, 1.5]}'
    assert dispatch.to_json(b"\x00\x01") == "[0, 1]"


def test_list_methods_works_while_disconnected():
    described = dispatch.describe(DG1022Z, ({"name": "reconnect",
                                             "signature": "()", "doc": ""},))
    names = {d["name"] for d in described}
    for expected in ("dcinit", "update", "move", "sininit", "sinupdate",
                     "offsets", "position", "command", "query", "snapshot"):
        assert expected in names, f"{expected} is not discoverable"
    assert not any(n.startswith("_") for n in names)
    assert "reconnect" in names
    sig = next(d["signature"] for d in described if d["name"] == "update")
    assert not sig.startswith("(self"), sig


# ------------------------------------------------- gateway JSON -> message
def _conversion():
    """Import the gateway's conversion module with rosidl stubbed out."""
    if "scopio_gateway.conversion" in sys.modules:
        return sys.modules["scopio_gateway.conversion"]
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "scopio_gateway"))
    rosidl = types.ModuleType("rosidl_runtime_py")
    rosidl.message_to_ordereddict = dict
    rosidl.set_message_fields = lambda msg, data: [setattr(msg, k, v)
                                                   for k, v in data.items()]
    utils = types.ModuleType("rosidl_runtime_py.utilities")
    utils.get_message = utils.get_service = lambda t: None
    sys.modules["rosidl_runtime_py"] = rosidl
    sys.modules["rosidl_runtime_py.utilities"] = utils
    from scopio_gateway import conversion
    return conversion


class FakeControls:
    """Stands in for SetCameraControls.Request: floats defaulting to 0.0."""

    _FIELDS = {"red_gain": "double", "blue_gain": "double", "contrast": "double"}

    def __init__(self):
        for name in self._FIELDS:
            setattr(self, name, 0.0)

    @classmethod
    def get_fields_and_field_types(cls):
        return dict(cls._FIELDS)


def test_a_partial_service_body_leaves_other_floats_alone():
    """The bug this closes: POST camera/set_controls {"contrast": 1.2} built a
    request with red_gain=0.0 -- a command to zero that gain, not "unchanged".
    An EMPTY body did it to every field at once."""
    conversion = _conversion()
    import math as _math

    msg = conversion.build_msg(FakeControls, {"contrast": 1.2}, nan_for_missing=True)
    assert msg.contrast == 1.2
    assert _math.isnan(msg.red_gain) and _math.isnan(msg.blue_gain)

    empty = conversion.build_msg(FakeControls, {}, nan_for_missing=True)
    assert all(_math.isnan(getattr(empty, f)) for f in FakeControls._FIELDS)

    # Explicit null still means "unchanged"; action goals keep the 0.0 default.
    assert _math.isnan(conversion.build_msg(FakeControls, {"contrast": None}).contrast)
    assert conversion.build_msg(FakeControls, {"contrast": 1.2}).red_gain == 0.0


# ------------------------------------------------------- camera server
def _camera_server():
    """Import the camera server with picamera2 stubbed out."""
    if "pi_camera_server" in sys.modules:
        return sys.modules["pi_camera_server"]
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    sys.path.insert(0, os.path.join(root, "camera_server"))
    for name, attrs in [("picamera2", {"Picamera2": object}),
                        ("picamera2.encoders", {"MJPEGEncoder": object}),
                        ("picamera2.outputs", {"FileOutput": object})]:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
    import pi_camera_server
    return pi_camera_server


class MonoCamera:
    """A monochrome sensor: no AwbEnable, no ColourGains, no Saturation."""

    camera_controls = {"AeEnable": None, "ExposureTime": None, "AnalogueGain": None,
                       "FrameDurationLimits": None, "Brightness": None,
                       "Contrast": None, "Sharpness": None}

    def __init__(self):
        self.applied = None

    def set_controls(self, c):
        for name in c:
            if name not in self.camera_controls:
                raise RuntimeError(f"Control {name} is not advertised by libcamera")
        self.applied = c

    def capture_metadata(self):
        return {}


def test_mono_sensor_does_not_reject_the_whole_request():
    """The bug: one unsupported control (AwbEnable on a mono sensor) raised out
    of the handler, so NOTHING was applied, the socket hung up, and the caller
    retried forever."""
    cs = _camera_server()
    cam = MonoCamera()
    cs.picam2 = cam
    try:
        cs.apply_controls({"exposure": 12000, "analogue_gain": 2.0, "contrast": 1.5,
                           "red_gain": 2.4, "blue_gain": 2.5, "framerate": 30})
        assert cam.applied is not None, "nothing was applied"
        assert "AwbEnable" not in cam.applied and "ColourGains" not in cam.applied
        # ...and everything the sensor DOES have still went through.
        assert cam.applied["ExposureTime"] == 12000
        assert cam.applied["AnalogueGain"] == 2.0
        assert cam.applied["Contrast"] == 1.5
        assert "FrameDurationLimits" in cam.applied
        # White balance answers instead of raising.
        assert "error" in cs.do_white_balance()
    finally:
        cs.picam2 = None


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} checks passed.")


if __name__ == "__main__":
    main()
