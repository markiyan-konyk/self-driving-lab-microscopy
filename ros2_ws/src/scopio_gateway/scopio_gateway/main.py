"""Entry point: `ros2 run scopio_gateway gateway`.

Starts the rclpy node + executor in a background thread, then runs uvicorn
(and its asyncio loop) on the main thread.

Env:
  SCOPIO_GATEWAY_PORT   listen port          (default 8000)
  SCOPIO_GATEWAY_HOST   bind address         (default 0.0.0.0)
  SCOPIO_API_KEYS_FILE  API keys JSON        (default /secrets/api_keys.json)
  CAMERA_URL            camera server        (default http://127.0.0.1:8081,
                                              empty = no camera)
"""

import os

import uvicorn

from .app import app
from .ros_bridge import bridge


def main():
    host = os.environ.get("SCOPIO_GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("SCOPIO_GATEWAY_PORT", "8000"))

    bridge.start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        bridge.shutdown()


if __name__ == "__main__":
    main()
