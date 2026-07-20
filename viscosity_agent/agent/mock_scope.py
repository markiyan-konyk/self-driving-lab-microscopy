"""MockScope -- a synthetic microscope for ``--dry-run``.

Renders dark frames with bright Gaussian "beads" that undergo real Brownian
motion, and exposes the exact subset of the ``scopio_client.Scopio`` surface the
tools use (health/status, stage, camera, calibration, stream_frames). It lets the
whole graph -- survey, acquire, track, estimate, critique, report -- run end to
end with NO hardware, and against a KNOWN ground-truth viscosity so the recovered
value can be checked.

Why the result is trustworthy despite jittery timing: on every frame request the
beads are advanced by Brownian steps sized to the REAL elapsed wall-clock time
since the previous frame (variance 2*D*dt per axis), then the pixel conversion
uses whatever calibration the agent set. So the pixel size cancels and the
recovered diffusion coefficient -> eta_true regardless of the calibration value
or the (irregular) frame timing -- exactly the property the real capture path
relies on.

Ground truth: eta_true = 8.9e-4 Pa.s (water @ 25 C) for 0.5 um-radius beads.
"""

import math
import random
import threading
import time

import cv2
import numpy as np

KB = 1.380649e-23
FRAME_W, FRAME_H = 640, 480


def _diffusion_from_eta(eta, T, r):
    return KB * T / (6.0 * math.pi * eta * r)


class _NS:
    """Tiny attribute bag so mock.stage.jog(...) etc. mirror the real namespaces."""
    def __init__(self, **fns):
        self.__dict__.update(fns)


class MockScope:
    def __init__(self, eta_true=8.9e-4, temperature_K=298.15,
                 bead_radius_m=0.5e-6, seed=7):
        self.eta_true = float(eta_true)
        self.T = float(temperature_K)
        self.r = float(bead_radius_m)
        self.D_true = _diffusion_from_eta(self.eta_true, self.T, self.r)

        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()

        # stage position in Sangaboard steps
        self._pos = {"x": 0, "y": 0, "z": 0}
        # calibration starts UNSET -- exercises the human-input gate
        self._um_per_px = None
        # camera controls
        self._controls = {
            "framerate": 30.0, "exposure": 8000.0, "analogue_gain": 1.0,
            "red_gain": 2.4, "blue_gain": 2.5, "contrast": 1.0,
            "saturation": 1.0, "brightness": 0.0, "sharpness": 1.0,
        }
        self._focus = 120.0                # variance-of-Laplacian proxy
        self._beads_px = None              # (N,2) float pixel positions
        self._bead_amp = None              # per-bead brightness
        self._bead_sigma = None            # per-bead blob sigma (px)
        self._last_t = None
        self._regen_beads()

        # public namespaces
        self.stage = _NS(jog=self._jog, move_abs=self._move_abs,
                         position=self._position)
        self.camera = _NS(get_controls=self._get_controls,
                          set_controls=self._set_controls,
                          set_framerate=self._set_framerate,
                          white_balance=self._white_balance,
                          autofocus=self._autofocus,
                          focus_metric=self._focus_metric)
        self.calibration = _NS(get=self._cal_get, set=self._cal_set)

    # ------------------------------------------------------------ scene model
    def _expected_beads(self, x, y):
        """Bead count as a smooth function of stage position (a 'good patch')."""
        gx, gy, scale = 500.0, 0.0, 650.0
        bump = 4.0 * math.exp(-((x - gx) ** 2 + (y - gy) ** 2) / (2 * scale ** 2))
        base = 7.0 + bump
        return max(2, int(round(base + self._rng.normal(0, 0.6))))

    def _regen_beads(self):
        """Populate a fresh bead field for the current FOV (called on jog)."""
        n = self._expected_beads(self._pos["x"], self._pos["y"])
        margin = 40
        xs = self._rng.uniform(margin, FRAME_W - margin, n)
        ys = self._rng.uniform(margin, FRAME_H - margin, n)
        self._beads_px = np.column_stack([xs, ys]).astype(float)
        self._bead_amp = self._rng.uniform(170, 220, n)
        self._bead_sigma = np.full(n, 2.4)
        # occasionally seed one bright doublet so clump metrics aren't trivially 0
        if n >= 6 and self._rng.random() < 0.5:
            i = int(self._rng.integers(0, n))
            self._beads_px[i] = self._beads_px[(i + 1) % n] + self._rng.uniform(-4, 4, 2)
            self._bead_amp[i] *= 1.4
            self._bead_sigma[i] = 3.4
        self._last_t = None

    def _advance(self, dt):
        """Advance all beads by Brownian steps for real elapsed time dt (s)."""
        if dt <= 0 or self._beads_px is None:
            return
        um_per_px = self._um_per_px or 0.5
        px_size_m = um_per_px * 1e-6
        sigma_px = math.sqrt(2.0 * self.D_true * dt) / px_size_m
        steps = self._rng.normal(0.0, sigma_px, self._beads_px.shape)
        self._beads_px += steps
        # keep inside the frame (reflect) -- rarely triggers over a short clip
        np.clip(self._beads_px[:, 0], 2, FRAME_W - 3, out=self._beads_px[:, 0])
        np.clip(self._beads_px[:, 1], 2, FRAME_H - 3, out=self._beads_px[:, 1])

    def _render(self):
        """Draw the current bead field to a BGR uint8 JPEG-able frame."""
        frame = self._rng.normal(10, 3, (FRAME_H, FRAME_W, 3)).clip(0, 255)
        yy, xx = np.mgrid[0:FRAME_H, 0:FRAME_W]
        green = np.zeros((FRAME_H, FRAME_W), dtype=float)
        for (bx, by), amp, sig in zip(self._beads_px, self._bead_amp, self._bead_sigma):
            x0, x1 = max(0, int(bx - 10)), min(FRAME_W, int(bx + 11))
            y0, y1 = max(0, int(by - 10)), min(FRAME_H, int(by + 11))
            if x1 <= x0 or y1 <= y0:
                continue
            gx = xx[y0:y1, x0:x1] - bx
            gy = yy[y0:y1, x0:x1] - by
            green[y0:y1, x0:x1] += amp * np.exp(-(gx * gx + gy * gy) / (2 * sig * sig))
        # beads are brightest in green (channel 1), the detection channel
        frame[:, :, 1] += green
        frame[:, :, 0] += 0.35 * green
        frame[:, :, 2] += 0.35 * green
        return frame.clip(0, 255).astype(np.uint8)

    def _encode(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return buf.tobytes() if ok else b""

    # ------------------------------------------------------------ Scopio surface
    def health(self):
        return {"ok": True, "ros_ok": True, "camera_ok": True,
                "auth_configured": True, "mock": True}

    def status(self):
        return {"telemetry": {}, "mock": True}

    def _jog(self, dx=0, dy=0, dz=0):
        with self._lock:
            self._pos["x"] += int(dx)
            self._pos["y"] += int(dy)
            self._pos["z"] += int(dz)
            self._regen_beads()
        return {"success": True, "message": "ok", **self._pos}

    def _move_abs(self, x, y, z):
        with self._lock:
            self._pos = {"x": int(x), "y": int(y), "z": int(z)}
            self._regen_beads()
        return {"success": True, "message": "ok", **self._pos}

    def _position(self):
        return {**self._pos, "connected": True}

    def _get_controls(self):
        return dict(self._controls, measured_fps=self._controls["framerate"])

    def _set_controls(self, **controls):
        for k, v in controls.items():
            if k in self._controls and v is not None:
                self._controls[k] = float(v)
        return dict(self._controls)

    def _set_framerate(self, fps):
        return self._set_controls(framerate=float(fps))

    def _white_balance(self):
        return {"success": True, "red_gain": self._controls["red_gain"],
                "blue_gain": self._controls["blue_gain"]}

    def _autofocus(self, z_range=2000, steps=15, settle_s=0.2, on_feedback=None,
                   timeout=600.0):
        self._focus = 150.0
        return {"status": "SUCCEEDED",
                "result": {"message": "focused (mock)", "best_z": self._pos["z"]}}

    def _focus_metric(self):
        return {"focus": self._focus}

    def _cal_get(self):
        if self._um_per_px is None:
            return {"has_um_per_px": False, "um_per_px": float("nan")}
        return {"has_um_per_px": True, "um_per_px": self._um_per_px}

    def _cal_set(self, **values):
        v = values.get("um_per_px")
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            self._um_per_px = float(v)
        return {"success": True, "um_per_px": self._um_per_px}

    def stream_frames(self, chunk_size=16384):
        """Generator of JPEG bytes, throttled to the current framerate + jitter."""
        while True:
            with self._lock:
                now = time.monotonic()
                dt = 0.0 if self._last_t is None else now - self._last_t
                self._last_t = now
                self._advance(dt)
                frame = self._render()
            yield self._encode(frame)
            fps = max(1.0, self._controls["framerate"])
            base = 1.0 / fps
            time.sleep(base * (1.0 + random.uniform(-0.08, 0.08)))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
