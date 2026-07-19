# SCOPIO — run the microscope from a Windows laptop

Complete from-zero guide. Since the API-gateway restructure, the client side
is **trivial**: the laptop needs Python and the network — **no WSL2, no
Docker, no ROS, no DDS peer files, no firewall rules, no static IPs.**

### What runs where

| Machine | Runs | How |
|---|---|---|
| **Raspberry Pi** | ROS 2 graph + camera server + **API gateway** | one `docker compose up -d` |
| **Windows laptop** | UI / galvo_draw / your scripts | plain Python (API clients) |
| **Any device** | just viewing/using the UI | a web browser → the laptop |

Clients talk to the gateway at `http://<pi-ip>:8000` with an **API key**.
The full command reference is [`docs/API.md`](docs/API.md); interactive docs
live at `http://<pi-ip>:8000/docs`.

---

# Part 1 — Raspberry Pi (do once)

### 1.1 Get the code
```bash
cd ~
git clone https://github.com/markiyan-konyk/self-driving-lab-microscopy.git self-driving-lab
cd ~/self-driving-lab && git checkout remake && git pull
```

### 1.2 Pin the AWG (galvo) if attached
```bash
# find the VISA string once:  python3 -m pyvisa info   (or check galvo_tests/)
echo 'GALVO_RESOURCE=USB0::6833::1602::DG1ZA278M01038::0::INSTR' > ros2_ws/.env
```

### 1.3 Generate API keys (one per client app/person)
```bash
python3 ros2_ws/scripts/generate_api_key.py laptop
# prints a 48-hex key -- copy it somewhere safe; you'll use it in Part 2.
```

### 1.4 Start everything
```bash
cd ros2_ws
docker compose up -d --build        # ROS graph + camera + gateway, all of it
sudo systemctl enable docker        # so it all returns after a reboot
```

### 1.5 Verify on the Pi
```bash
curl http://127.0.0.1:8000/api/v1/health
#  {"ok": true, "ros_ok": true, "camera_ok": true, "auth_configured": true, ...}
curl http://127.0.0.1:8081/controls          # camera server (loopback only)
```

**If `camera_ok` is false**: check `docker logs scopio-camera`. If
picamera2/libcamera won't run inside the container on your Pi, use the systemd
fallback — identical result, nothing else changes:
```bash
docker compose stop camera
cd ~/self-driving-lab/camera_server && sudo ./install_systemd.sh
```
(Then comment the `camera` service out of `ros2_ws/docker-compose.yml` so
`up` doesn't restart it.)

### 1.6 Find the Pi's IP
```bash
hostname -I        # e.g. 192.168.1.42 -- clients will use http://192.168.1.42:8000
```
Any network works: lab LAN, WiFi, or a direct ethernet cable (if you use a
direct cable, give both ends static IPs as before — but this is now an
*option*, not a requirement).

---

# Part 2 — Windows laptop

### 2.1 Prereqs
- Python 3.10+ (`winget install Python.Python.3.12`)
- the repo: `git clone <repo-url>` then `git checkout remake`

### 2.2 Install and configure
```powershell
cd self-driving-lab-microscopy
pip install -r ui\requirements.txt          # includes the scopio_client SDK

$env:SCOPIO_URL     = "http://<pi-ip>:8000"
$env:SCOPIO_API_KEY = "<key from Part 1.3>"
```
(Set them permanently with `setx SCOPIO_URL ...` / `setx SCOPIO_API_KEY ...`.)

### 2.3 Sanity check
```powershell
python -c "from scopio_client import Scopio; import os; print(Scopio(os.environ['SCOPIO_URL'], os.environ['SCOPIO_API_KEY']).health())"
```

### 2.4 Run the apps
```powershell
python ui\run_ui.py                # web UI      -> http://localhost:8080
python galvo_draw\app.py           # laser draw  -> http://localhost:8090
```
Teammates on the network can open the UI in their browser via your laptop's
IP — or run their own copy with their own key.

---

# Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach the microscope` | Wrong IP / Pi off / different network. `ping <pi-ip>`, then `curl http://<pi-ip>:8000/api/v1/health` from the laptop. |
| `401 Missing or invalid API key` | Key typo, or the key was never generated on the Pi. Re-run `generate_api_key.py`; the gateway picks the file up instantly. |
| `health` ok but `camera_ok: false` | Camera server down on the Pi — see Part 1.5. |
| Video black in the UI | Same as above, or the camera ribbon cable. `curl http://127.0.0.1:8081/controls` on the Pi. |
| `504` on stage/galvo calls | The node is up but the hardware isn't answering: Sangaboard USB/serial cable, or AWG not pinned (`GALVO_RESOURCE` in `ros2_ws/.env`, then `docker compose up -d` again). Check `docker logs scopio`. |
| "AWG unavailable" | Same as above; confirm with `curl -H "X-API-Key: $KEY" -d '{"command":"*IDN?"}' -H "Content-Type: application/json" http://<pi>:8000/api/v1/service/awg/query`. |
| Everything died after a Pi reboot | `sudo systemctl enable docker` (once); the compose services carry `restart: unless-stopped`. |

> **Note for time travelers:** older revisions of this file described a WSL2 +
> Docker-in-WSL + Fast-DDS-unicast-peers setup. That whole world is gone — the
> gateway replaced it. If you see references to `dds/*.xml`, UDP 7400-7600 or
> `CAMERA_MJPEG_URL`, you're reading an old commit.
