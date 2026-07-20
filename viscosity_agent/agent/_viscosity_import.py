"""Bridge onto the repo's existing ``viscosity/`` package.

The validated detection (trackpy), linking, and Stokes-Einstein physics already
live in ``../../viscosity/`` (``track.py``, ``calculation.py``, ``main.py``).
Rather than re-implement or copy them, we put that directory on ``sys.path`` and
import the functions we need. This app then wraps them with an autonomy layer.

VENDORING TODO: when this app moves to a separate clients repo (see the repo
split discussion in docs/), copy ``track.py`` + ``calculation.py`` into
``viscosity_agent/agent/_vendor/`` and drop this shim. Until then a single
sys.path entry keeps one source of truth for the physics.

Exposes:
    detect, link, DEFAULTS          (from viscosity/track.py)
    estimate_viscosity              (from viscosity/calculation.py)
    well_tracked_particles, summarise  (from viscosity/main.py)
"""

import os
import sys

# viscosity_agent/agent/_viscosity_import.py -> repo root is two levels up.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
_VISCOSITY_DIR = os.path.join(_REPO_ROOT, "viscosity")

if not os.path.isdir(_VISCOSITY_DIR):
    raise ImportError(
        f"viscosity_agent depends on the repo's viscosity/ package but it was "
        f"not found at {_VISCOSITY_DIR}. Run this app from inside the "
        f"self-driving-lab-microscopy checkout."
    )

if _VISCOSITY_DIR not in sys.path:
    sys.path.insert(0, _VISCOSITY_DIR)

# track.py pins BLAS/OpenMP threads to 1 at import (before numpy) -- importing it
# here, early, means every worker process we later spawn inherits that too.
from track import detect, link, choose_percentile, DEFAULTS   # noqa: E402
from calculation import estimate_viscosity, KB                # noqa: E402
from main import well_tracked_particles, summarise            # noqa: E402

__all__ = [
    "detect", "link", "choose_percentile", "DEFAULTS",
    "estimate_viscosity", "KB",
    "well_tracked_particles", "summarise",
    "VISCOSITY_DIR",
]

VISCOSITY_DIR = _VISCOSITY_DIR
