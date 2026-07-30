# scopio_mcp — the microscope as an MCP server

Exposes the SCOPIO microscope to **Claude Code** (or any MCP client) as a set of
tools. It is a thin client of the existing HTTP gateway, built on
`scopio_client` — exactly like `ui/` and `galvo_draw/`. The gateway and the API
are unchanged; nothing here is required by the other apps.

```
Claude Code  --stdio/MCP-->  scopio_mcp/server.py  --HTTP-->  gateway (Pi)  -->  ROS 2
```

## Setup

Three things, whatever folder you are in: the packages installed, a `.env` with
the microscope's address and your key, and a `.mcp.json` telling Claude Code to
start the server.

### In this repo

```bash
pip install -e ./scopio_client -r scopio_mcp/requirements.txt
cp scopio_mcp/.env.example scopio_mcp/.env      # then fill in URL + key
```

The repo's `.mcp.json` is already there — start `claude` in the repo root and
approve the server.

### In a folder of your own

Copy `scopio_mcp/` and `scopio_client/` into it (keep them side by side — the
install below links to the SDK in place), then:

```bash
cd <your folder>
python -m venv .venv && .venv\Scripts\activate         # macOS/Linux: source .venv/bin/activate
pip install -e ./scopio_client -r scopio_mcp/requirements.txt
copy scopio_mcp\.env.example scopio_mcp\.env           # then fill in URL + key

claude mcp add -s project scopio -- .venv\Scripts\python.exe scopio_mcp\server.py
claude
```

`claude mcp add` writes the `.mcp.json` for you. Point it at **the interpreter
that has the requirements** — a bare `python` only works if that is already the
one on your PATH, which it is not inside a venv you did not activate. Everything
is relative to that folder, so the whole thing is copyable and committable
(minus `.env` and `recordings/`).

### The key

`.env` sits next to `server.py` and takes the same variables as the other
clients. Real environment variables win over it, so CI can set them instead.

```
SCOPIO_URL=http://<pi-ip>:8000
SCOPIO_API_KEY=<mint on the Pi with ros2_ws/scripts/generate_api_key.py>
```

Mint the key **on the Pi**, once per person or app:

```bash
python3 ros2_ws/scripts/generate_api_key.py alice     # prints the key
```

The gateway hot-reloads its key file, so no restart. Keep `.env` out of git.

### Check it

Run `/mcp` in Claude Code — the tools appear as `scopio - <name>`. Then ask for
`status`: it answers with live telemetry if the Pi is up, and says exactly what
is missing if it is not.

## Tools

| Tool | What it does |
|---|---|
| `describe_instrument` | Live capability map: services, topics, actions + schemas, and every galvo/temperature driver method. Start here. |
| `status` | Health plus latest stage/camera/temperature/AWG/calibration telemetry. |
| `call_service` | Any ROS service by path (`stage/jog`, `calibration/set`, ...). |
| `send_goal` | Long actions: `camera/autofocus`, `stage/move_path`, `scan_region`. |
| `stage_move` | Relative or absolute stage moves, in steps. |
| `camera_controls` | Read or partially write camera settings. |
| `grab_frame` | One frame, downscaled, returned as an image Claude can *see*. |
| `white_balance` | One-shot AWB, then locked. Run it when the light changes. |
| `focus_metric` | Cheap sharpness number for focus sweeps. |
| `record_clip` | Record N seconds to `recordings/<name>/00000.jpg…` + measured fps. |
| `instrument_call` | Any driver method on `galvo` or `temperature`. |
| `galvo_scpi` | One raw SCPI command to the AWG (`?` ⇒ query). |

## Where recordings go

`record_clip` writes to `recordings/<name>/` **in the client's working
directory** — the folder you started Claude Code in — never next to this server.
That is what lets the agent read back frames it just recorded with its own file
tools, and it keeps this repo clean when the two live apart. Add `recordings/`
to the working folder's `.gitignore`.

## Test

```bash
pip install fastapi uvicorn                  # the mock gateway
python scopio_mcp/test_scopio_mcp.py         # no microscope needed
```

Calls every tool against the mock gateway from `scopio_client`'s test. Run it
after touching `server.py` or bumping the `mcp` SDK.

## Safety

`call_service`, `instrument_call` and `galvo_scpi` are unrestricted by design —
they mirror the gateway's generic surface. The ROS nodes currently enforce **no
range limits** on stage, temperature or AWG. Add clamps in the nodes (and/or
scoped API keys in the gateway) before running an agent unattended; a limit
enforced only by a prompt is not a limit.
