"""Locate the repo's existing Python modules so the ROS nodes can reuse them.

The ROS nodes deliberately reuse the *clean, hardware-facing* code that already
exists and is validated:

  * microscope/galvo.py   -> Galvo driver (DG1022Z)
  * microscope/tweezer.py -> laser geometry / zeroing
  * viscosity/track.py    -> trackpy parameters + single-frame locate

rather than vendoring copies. The Flask-coupled parts (camera.py / controls.py,
which carry module-global app state) are NOT reused; the camera and stage nodes
re-implement those leanly for ROS.

In the container the repo is mounted at $SCOPIO_REPO (default /workspace). When
running outside a container we walk up from this file to find it.
"""

import os
import sys


def repo_root():
    """Return the path to the repo root (the dir containing microscope/ and
    viscosity/), or None if it cannot be located."""
    env = os.environ.get("SCOPIO_REPO")
    candidates = []
    if env:
        candidates.append(env)
    # Walk up from this file: .../ros2_ws/src/scopio_microscope/scopio_microscope/
    here = os.path.dirname(os.path.abspath(__file__))
    up = here
    for _ in range(8):
        up = os.path.dirname(up)
        candidates.append(up)
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "microscope")) \
                and os.path.isdir(os.path.join(c, "viscosity")):
            return c
    return None


def ensure_on_path():
    """Put the repo's microscope/ and viscosity/ dirs on sys.path. Returns the
    repo root or None. Safe to call repeatedly."""
    root = repo_root()
    if root is None:
        return None
    for sub in ("microscope", "viscosity"):
        p = os.path.join(root, sub)
        if p not in sys.path:
            sys.path.insert(0, p)
    return root
