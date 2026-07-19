"""scopio_client -- Python SDK for the SCOPIO microscope API gateway.

    from scopio_client import Scopio

    scope = Scopio("http://<pi-ip>:8000", api_key="<key>")
    scope.stage.jog(dz=100)
    scope.camera.autofocus()
    for jpeg in scope.stream_frames():
        ...

See docs/API.md in the repository for the full command manual.
"""

from .client import Scopio
from .errors import ScopioError

__all__ = ["Scopio", "ScopioError"]
