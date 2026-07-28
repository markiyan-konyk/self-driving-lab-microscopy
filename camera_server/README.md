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
