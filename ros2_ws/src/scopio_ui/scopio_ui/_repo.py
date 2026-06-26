"""Locate the repo so the gateway can serve microscope/frontend and import the
client-side galvo geometry helper (microscope/galvo_geometry.py).

In the container the repo is mounted at $SCOPIO_REPO (default /workspace);
outside a container we walk up from this file to find the dir that contains
microscope/. Mirrors scopio_microscope/repo_paths.py.
"""

import os
import sys


def repo_root():
    env = os.environ.get("SCOPIO_REPO")
    candidates = [env] if env else []
    here = os.path.dirname(os.path.abspath(__file__))
    up = here
    for _ in range(8):
        up = os.path.dirname(up)
        candidates.append(up)
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "microscope")):
            return c
    return None


def ensure_on_path():
    root = repo_root()
    if root is None:
        return None
    p = os.path.join(root, "microscope")
    if p not in sys.path:
        sys.path.insert(0, p)
    return root
