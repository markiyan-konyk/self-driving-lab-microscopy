# SCOPIO — self-driving-lab microscope

An OpenFlexure-based microscope (Sangaboard XYZ stage, Pi camera, galvo-steered
laser via a Rigol AWG) turned into a **networked instrument**: everything that
senses or effectuates lives on the Raspberry Pi as a ROS 2 graph, and the
outside world talks to it through an **HTTP/WebSocket API gateway with API-key
auth**. Client programs need no ROS, no Docker, no DDS — just a URL and a key.

```
                 ┌───────────────── Raspberry Pi ─────────────────┐
                 │  docker compose up -d   (ros2_ws/)             │
                 │                                                │
                 │  camera server ◄─loopback─┐                    │
                 │  (picamera2, :8081)       │                    │
                 │        ▲                  │                    │
                 │        │ MJPEG/HTTP       │                    │
                 │  ROS 2 graph /scopio ◄──► API gateway (:8000)  │
                 │  camera·stage·galvo·      HTTP + WS + API key  │
                 │  temperature·calibration  │                    │
                 └───────────────────────────┼────────────────────┘
                                             │  any network
              ┌───────────────┬──────────────┼────────────────┐
              ▼               ▼              ▼                ▼
        ui/ (web UI)   galvo_draw/     your scripts      future agents
                        (laser art)   (scopio_client)
```

## The pieces

| Directory | What it is |
|---|---|
| [`ros2_ws/`](ros2_ws/) | the Pi backend: ROS 2 driver nodes (`scopio_microscope`), the frozen interface contract (`scopio_interfaces`), and the API gateway (`scopio_gateway`) — one `docker compose up -d` |
| [`camera_server/`](camera_server/) | the single owner of the Pi camera; MJPEG + controls on loopback :8081 (compose service, systemd fallback) |
| [`scopio_client/`](scopio_client/) | pip-installable Python SDK: `Scopio(url, api_key)` |
| [`docs/API.md`](docs/API.md) | **the developer manual** — every command with JSON/curl/SDK examples |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | architecture rationale (why a dumb galvo passthrough, why backend/UI split, etc.) |
| [`ui/`](ui/) | reference web UI (Flask), a pure API client, records video locally |
| [`galvo_draw/`](galvo_draw/) | draw shapes with the laser (arbitrary-waveform vector display) |
| `viscosity/` | offline bead-tracking/analysis pipeline — where **all** image analysis lives; the Pi backend does none |
| `galvo_tests/` | standalone hardware bench scripts for calibrating the galvo (independent of everything above) |
| `temperature.py`, `galvo.py` | the instrument driver classes (TC LAB controller, Rigol AWG). The backend runs **copies** of these in `ros2_ws/…/scopio_microscope/drivers/` and exposes every method of them over the API |
| `temperature_test.py` | prove the temperature controller works with nothing but pyvisa — run this before blaming the stack |
| `instrument_scan.py` | when VISA "can't see" an instrument: shows every USB device with its interface **class** (USBTMC vs virtual-COM decides whether pyvisa can *ever* list it), the bound kernel driver, the serial ports, and `--probe`s each for `*IDN?` |

## Quick start

**On the Pi** (once):
```bash
cd ros2_ws
cp .env.example .env                            # which VISA instrument is which
python3 scripts/generate_api_key.py laptop      # note the printed key
docker compose up -d --build                    # graph + camera + gateway
```

**On any computer** (Windows/macOS/Linux, same network):
```bash
pip install -e ./scopio_client
python -c "from scopio_client import Scopio; \
           s = Scopio('http://<pi-ip>:8000', api_key='<key>'); \
           print(s.health()); s.stage.jog(dz=100)"
```

Or run the UI: see [`ui/README.md`](ui/README.md). Full client walkthrough
from a fresh Windows laptop: [`docs/WINDOWS_CLIENT.md`](docs/WINDOWS_CLIENT.md).

## Design in one paragraph

The Pi only **senses and effectuates**; decisions live off-board
([`docs/DECISIONS.md`](docs/DECISIONS.md)). The ROS interfaces are additive-only,
and the gateway maps them generically to JSON — so a new node (the temperature
controller was the latest) becomes remotely usable the moment it launches, with
zero gateway changes and automatic listing in `GET /api/v1/interfaces`.
Instrument nodes go one step further: they expose their whole **driver class**
(`temperature/call`, `awg/call`), so any app can use any capability of the
instrument without the ROS contract changing at all. The API key is the lock;
the gateway is the only door (the camera server and the DDS graph never face
the LAN).

Status: restructured for the API-gateway architecture on branch `remake`;
gateway + clients validated against the no-hardware graph, on-Pi hardware
bring-up per [`ros2_ws/docs/BRINGUP.md`](ros2_ws/docs/BRINGUP.md).
