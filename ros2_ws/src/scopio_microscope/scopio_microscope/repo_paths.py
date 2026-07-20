"""Locate the repo's existing Python modules so the ROS nodes can reuse them.

The only thing still reused this way is:

  * viscosity/track.py -> trackpy parameters (DEFAULTS) for tracker_node, so
    live tracking uses the same tuning as the offline viscosity pipeline.

(The legacy `microscope/` monolith that camera_node/galvo_node's docstrings
credit as their design inspiration has since been deleted from the repo --
those nodes never imported it at runtime, only ported its logic. Only
tracker_node has a live import, and only of viscosity/.)

In the container the repo is mounted at $SCOPIO_REPO (default /workspace). When
running outside a container we walk up from this file to find it.
"""

import os
import sys


def repo_root():
    """Return the path to the repo root (the dir containing viscosity/), or
    None if it cannot be located."""
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
        if c and os.path.isdir(os.path.join(c, "viscosity")):
            return c
    return None


def ensure_on_path():
    """Put the repo's viscosity/ dir on sys.path. Returns the repo root or
    None. Safe to call repeatedly."""
    root = repo_root()
    if root is None:
        return None
    p = os.path.join(root, "viscosity")
    if p not in sys.path:
        sys.path.insert(0, p)
    return root
