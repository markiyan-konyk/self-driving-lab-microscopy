"""DG1022Z (Rigol DG1000Z series) SCPI builders for the galvo laser display.

The galvo can't be blanked, so we never raster -- we drive the two mirrors with
a single, looping **arbitrary waveform**: X on CH1, Y on CH2, same length and
loop rate, phase-aligned. The AWG loops the path in hardware, so the eye sees a
steady vector image (persistence of vision). Amplitude (Vpp) scales the size.

Every function returns a *list of SCPI command strings*; the caller sends each
one through the ROS ``awg/write`` passthrough. No hardware libraries here.

Verified against the RIGOL DG1000Z Programming Guide (EN):
  * ``:SOURce<n>:DATA VOLATILE,<v>,...`` takes floats in [-1, 1], 8..16384 pts;
    +1 -> +Vpp/2, -1 -> -Vpp/2. Sending it AUTOMATICALLY switches the channel to
    output that volatile waveform (:TRACe:DATA[:DATA]) -- so no ":FUNC USER" is
    needed, and setting it isn't done here.
  * ``:SOURce<n>:FUNCtion:ARBitrary:MODE FREQ`` -> frequency mode, so
    ``:FREQuency`` is the whole-loop repetition rate (the flicker/refresh rate).
  * ``:OUTPut<n>:IMPedance INFinity`` -> High-Z. The galvo driver is high
    impedance; without this the AWG assumes a 50 ohm load and outputs half the
    requested voltage.
  * ``:APPLy:DC`` / ``:APPLy:SINusoid`` / ``:PHASe:SYNChronize`` per the guide.

To target a different instrument, this is the ONE file to change.
"""

V_MAX = 5.0


def clamp_vpp(vpp):
    # Peak excursion is +/- vpp/2; keep within the galvo's safe envelope.
    return max(0.0, min(2 * V_MAX, float(vpp)))


def arb_upload_cmds(ch, norm_values):
    """Load a normalized waveform (values in [-1, 1]) into the channel's volatile
    arbitrary memory. This command alone switches the channel to volatile-arb
    output, so it must be sent BEFORE the apply/frequency/amplitude commands."""
    data = ",".join(f"{max(-1.0, min(1.0, float(v))):.4f}" for v in norm_values)
    return [f":SOURce{ch}:DATA VOLATILE,{data}"]


def arb_apply_cmds(ch, vpp, freq_hz, offset_v=0.0):
    """Run the already-loaded volatile arb at a given size (Vpp) and loop rate
    (Hz). High-Z first (so Vpp isn't halved), frequency mode so :FREQ is the loop
    rate, then amplitude/offset, then enable the output."""
    return [
        f":OUTPut{ch}:IMPedance INFinity",
        f":SOURce{ch}:FUNCtion:ARBitrary:MODE FREQ",
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
    'stop' since the beam can't be switched off. The 1,1 are required freq/amp
    placeholders for :APPLy:DC; the third value is the DC offset (volts)."""
    return [
        f":SOURce1:APPLy:DC 1,1,{float(vx):.4f}",
        f":SOURce2:APPLy:DC 1,1,{float(vy):.4f}",
    ]


def test_circle_cmds(freq_hz=2.0, amp_vpp=1.0):
    """Draw a slow circle from the proven sine path (X sine, Y sine at +90 deg).
    Strongest link test: if the beam draws a circle, the whole ROS->AWG->galvo
    pipe and waveform generation work, so any remaining 'my drawing doesn't
    appear' is isolated to the arbitrary-waveform path. If even this fails, the
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
