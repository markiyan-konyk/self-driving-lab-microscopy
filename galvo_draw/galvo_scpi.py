"""DG1022Z SCPI builders for driving the galvo as a vector (laser-show) display.

The galvo can't be blanked, so we never raster -- we drive the two mirrors with
a continuous, looping **arbitrary waveform**: X on CH1, Y on CH2, same length and
frequency, phase-aligned. The AWG loops the path in hardware (kHz), so the eye
sees a steady vector image. Amplitude (Vpp) scales the drawing's size.

All functions return *lists of SCPI command strings*; the caller sends each one
through the ROS ``awg/write`` service. No hardware libraries here.

>>> VERIFY THESE AGAINST THE DG1022Z PROGRAMMING GUIDE <<<
The arbitrary-waveform command sequence below follows the Rigol DG1000Z series
docs, but exact tokens (`:DATA VOLATILE`, `:FUNCtion USER`, the apply order) can
differ by firmware. If the image doesn't appear, this file is the ONE place to
fix -- the app and the ROS layer don't change. Use the /test button in the app
(a plain `:VOLTage:OFFSet`, known-good) to confirm the link first.
"""

V_MAX = 5.0


def clamp_vpp(vpp):
    # Peak excursion is +/- vpp/2; keep within the galvo's safe envelope.
    return max(0.0, min(2 * V_MAX, float(vpp)))


def arb_upload_cmds(ch, norm_values):
    """Load a normalized waveform (values in [-1, 1]) into the channel's volatile
    arbitrary-waveform memory."""
    data = ",".join(f"{max(-1.0, min(1.0, float(v))):.4f}" for v in norm_values)
    return [f":SOURce{ch}:DATA VOLATILE,{data}"]


def arb_apply_cmds(ch, vpp, freq_hz, offset_v=0.0):
    """Select the loaded volatile arb and run it at a given size (Vpp) and loop
    rate (Hz). Sets High-Z impedance first -- the galvo driver is high-impedance,
    and without this the AWG assumes a 50 ohm load and outputs half the requested
    voltage (matches microscope/galvo.py, which sets INFinity at startup)."""
    return [
        f":OUTPut{ch}:IMPedance INFinity",
        f":SOURce{ch}:FUNCtion USER",
        f":SOURce{ch}:FREQuency {float(freq_hz):.3f}",
        f":SOURce{ch}:VOLTage {clamp_vpp(vpp):.4f}",
        f":SOURce{ch}:VOLTage:OFFSet {float(offset_v):.4f}",
        f":OUTPut{ch} ON",
    ]


def phase_sync_cmds():
    """Align CH1/CH2 phase so X and Y start their loop together (no drift/tilt)."""
    return [":SOURce1:PHASe:SYNChronize"]


def park_cmds(vx=0.0, vy=0.0):
    """Stop motion by holding both mirrors at a DC point (a single dot). Used as
    'stop' since the beam cannot be switched off. Uses the proven offset command."""
    return [
        ":SOURce1:APPLy:DC 1,1,0", f":SOURce1:VOLTage:OFFSet {float(vx):.4f}",
        ":SOURce2:APPLy:DC 1,1,0", f":SOURce2:VOLTage:OFFSet {float(vy):.4f}",
    ]


def test_point_cmds(vx=0.5, vy=0.0):
    """A known-good sanity command (plain DC offset) to confirm the ROS->AWG link
    independently of the arbitrary-waveform path."""
    return park_cmds(vx, vy)


def test_circle_cmds(freq_hz=2.0, amp_vpp=1.0):
    """Draw a slow circle using ONLY the proven sine path (the exact command family
    from galvo_tests/04_waveform.py). X = sine, Y = sine at +90 deg -> a circle.

    This is the strongest link test: if the beam draws a circle, then the ROS->AWG
    pipe, the instrument connection, and waveform *generation* all work -- so any
    remaining 'my drawing doesn't appear' is isolated to the arbitrary-waveform
    (:DATA VOLATILE / :FUNCtion USER) path only. If even this fails, the backend
    galvo_node isn't connected to the AWG (check /scopio/awg/status)."""
    return [
        ":OUTPut1:IMPedance INFinity",
        ":OUTPut2:IMPedance INFinity",
        f":SOURce1:APPLy:SINusoid {float(freq_hz)},{float(amp_vpp)},0",
        f":SOURce2:APPLy:SINusoid {float(freq_hz)},{float(amp_vpp)},0",
        ":SOURce1:PHASe 0",
        ":SOURce2:PHASe 90",
        ":SOURce1:PHASe:SYNChronize",
        ":OUTPut1 ON",
        ":OUTPut2 ON",
    ]
