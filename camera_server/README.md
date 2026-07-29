# camera_server -- the single owner of the Pi camera

`pi_camera_server.py` is the ONLY process that touches picamera2. It serves
MJPEG video + JSON camera controls on **loopback** `127.0.0.1:8081` (no auth,
so it must never face the LAN — the API gateway is the authenticated door).

Consumers, both over loopback:

- **API gateway** (`ros2_ws/src/scopio_gateway/`) — proxies `/stream.mjpg` and
  the camera controls to authenticated external clients.
- **camera_node** (ROS) — "bridge mode": ingests the stream, republishes it on
  `/scopio/image/compressed` (so the graph has frames), forwards the camera
  services here, and runs autofocus on the ingested frames.

## How it runs

**Default — docker compose service.** It is the `camera` service in
`ros2_ws/docker-compose.yml`, so `docker compose up -d` on the Pi starts it
with everything else (and `restart: unless-stopped` revives it on reboot).
The image is Debian bookworm + the Raspberry Pi apt archive (`python3-picamera2`).

Validate once on the real Pi (the one risky piece — libcamera in a container):

```bash
docker compose run --rm camera \
  python3 -c "from picamera2 import Picamera2; print(Picamera2.global_camera_info())"
curl http://127.0.0.1:8081/controls
```

**Fallback — systemd unit.** If libcamera won't behave in-container on your
Pi/kernel combo:

```bash
cd camera_server && sudo ./install_systemd.sh   # uses the OS python3-picamera2
docker compose stop camera                       # and comment it out of compose
```

Identical HTTP surface either way; nothing downstream changes.

## HTTP surface (loopback)

| Endpoint | Meaning |
|---|---|
| `GET /stream.mjpg` | live MJPEG video |
| `GET /controls` | current settings (merged with live exposure/gain metadata) |
| `POST /controls` | partial JSON update: `framerate, exposure, analogue_gain, red_gain, blue_gain, contrast, saturation, brightness, sharpness` |
| `POST /white_balance` | one-shot AWB; locks the measured gains |
| `GET /focus` | cheap focus metric (JPEG size) |

Env: `CAM_HOST` (127.0.0.1), `CAM_PORT` (8081), `CAM_W`/`CAM_H` (640x480).

## When there is no camera

The server **serves anyway** and retries opening the sensor in the background
(2 s, backing off to 30 s) — it does not exit, so it cannot crash-loop the
container and hide the reason. Every endpoint answers **503** with the cause:

```json
{"error": "IndexError: list index out of range", "cameras": []}
```

`"cameras": []` is the diagnostic: picamera2 imported and libcamera loaded, but
**no sensor is visible to this process**. In order of likelihood — the camera is
not visible on the *host* either (`rpicam-hello --list-cameras`: ribbon in the
DSI/display port rather than CSI, contacts facing the wrong way, or a sensor
`config.txt` does not auto-detect); or the host is fine and the container's
libcamera does not match the host kernel's camera stack, which is what the
systemd fallback above exists for.

A camera that appears later (replug, or the systemd unit releasing it) is picked
up by the retry loop with no restart. The gateway still reports
`camera_ok: false` throughout — 503 is deliberately not 200.
