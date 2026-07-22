"""Path resampling for the galvo vector display (speed = brightness).

The AWG plays a *single closed loop* of samples at a fixed rate, so the beam's
speed at any point is set purely by how densely that region is sampled: many
samples over a short arc -> slow beam -> bright; a few samples over a long jump
-> fast beam -> faint. Since the laser can't be blanked, we use this to make the
(unavoidable) travel lines between separate pen strokes faint, while the strokes
themselves stay bright and evenly lit.

Input strokes are already normalized to [-1, 1] (y up) by the browser; we only
resample here. Output is two equal-length arrays (x, y) in [-1, 1], uploaded as
the X (CH1) and Y (CH2) arbitrary waveforms.
"""

import numpy as np

ARB_MIN, ARB_MAX = 8, 16384      # DG1000Z volatile-arb point-count limits


def _clean(stroke):
    """(M,2) float array with consecutive duplicate points removed."""
    p = np.asarray(stroke, dtype=float).reshape(-1, 2)
    if len(p) <= 1:
        return p
    keep = np.ones(len(p), bool)
    keep[1:] = np.any(np.abs(np.diff(p, axis=0)) > 1e-9, axis=1)
    return p[keep]


def _arc_length(p):
    return float(np.sqrt((np.diff(p, axis=0) ** 2).sum(1)).sum()) if len(p) >= 2 else 0.0


def _resample_open(p, k):
    """Resample an open polyline to k points evenly spaced by arc length,
    including both endpoints. A zero-length stroke becomes a k-point dwell
    (a bright dot)."""
    k = max(1, int(k))
    if len(p) == 1 or _arc_length(p) == 0.0:
        return np.repeat(p[:1], k, axis=0)
    seg = np.sqrt((np.diff(p, axis=0) ** 2).sum(1))
    s = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.linspace(0.0, s[-1], k, endpoint=True)
    return np.column_stack([np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])])


def _clamp_n(n):
    return max(ARB_MIN, min(ARB_MAX, int(n)))


def _fit(x, y):
    """Pad tiny loops / trim huge ones to the AWG's accepted point range."""
    if len(x) < ARB_MIN:
        pad = ARB_MIN - len(x)
        x = np.concatenate([x, np.repeat(x[-1:], pad)])
        y = np.concatenate([y, np.repeat(y[-1:], pad)])
    elif len(x) > ARB_MAX:
        idx = np.linspace(0, len(x) - 1, ARB_MAX).astype(int)
        x, y = x[idx], y[idx]
    return x, y


def resample_closed(points, n):
    """Uniform mode: one continuous closed loop through all points, evenly
    spaced by arc length (travel lines are as bright as the strokes)."""
    p = _clean(points)
    if len(p) < 2:
        return None
    p = np.vstack([p, p[0]])                      # close the loop
    seg = np.sqrt((np.diff(p, axis=0) ** 2).sum(1))
    s = np.concatenate([[0.0], np.cumsum(seg)])
    if s[-1] == 0:
        return None
    t = np.linspace(0.0, s[-1], _clamp_n(n), endpoint=False)
    return np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])


def build_loop(strokes, n=1024, dim_travel=True, travel_pts=3):
    """Turn a list of strokes (each an (Mi,2) array in [-1,1]) into one closed
    loop (x, y) for the AWG. Returns None if there's nothing to draw.

    dim_travel=False -> uniform brightness (one arc-length loop through all pts).
    dim_travel=True  -> strokes bright and evenly lit; the connectors between
                        strokes (and the closing connector) get only travel_pts
                        samples each, so the beam whips across them and they fade.
    """
    strokes = [s for s in (_clean(s) for s in (strokes or [])) if len(s) >= 1]
    if not strokes:
        return None

    if not dim_travel:
        return resample_closed(np.vstack(strokes), n)

    n = _clamp_n(n)
    m = len(strokes)
    travel_pts = max(1, int(travel_pts))
    lengths = [_arc_length(s) for s in strokes]
    total = sum(lengths)

    # Reserve a few samples for each connector; spend the rest on the strokes,
    # split in proportion to arc length so every stroke is lit at the same
    # brightness (samples-per-unit-length is then ~constant across strokes).
    n_draw = max(2 * m, n - m * travel_pts)
    if total > 0:
        alloc = [max(2, int(round(n_draw * L / total))) for L in lengths]
    else:
        alloc = [max(2, n_draw // m)] * m         # all dwells (dots)

    xs, ys = [], []
    for i, s in enumerate(strokes):
        r = _resample_open(s, alloc[i])           # bright: dense, even spacing
        xs.extend(r[:, 0]); ys.extend(r[:, 1])
        nxt = strokes[(i + 1) % m]                # connector to next stroke;
        cx = np.linspace(s[-1, 0], nxt[0, 0], travel_pts + 2)[1:-1]  # the i==m-1
        cy = np.linspace(s[-1, 1], nxt[0, 1], travel_pts + 2)[1:-1]  # wrap closes
        xs.extend(cx); ys.extend(cy)              # faint: sparse -> fast beam

    return _fit(np.asarray(xs, float), np.asarray(ys, float))
