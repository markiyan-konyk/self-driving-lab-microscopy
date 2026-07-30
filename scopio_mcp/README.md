# scopio_mcp — the microscope as an MCP server

Exposes the SCOPIO microscope to **Claude Code** (or any MCP client) as a set of
tools. It is a thin client of the existing HTTP gateway, built on
`scopio_client` — exactly like `ui/` and `galvo_draw/`. The gateway and the API
are unchanged; nothing here is required by the other apps.

```
Claude Code  --stdio/MCP-->  scopio_mcp/server.py  --HTTP-->  gateway (Pi)  -->  ROS 2
```

## Setup

From the **repo root**:

```bash
pip install -e ./scopio_client -r scopio_mcp/requirements.txt
cp scopio_mcp/.env.example scopio_mcp/.env      # then fill in URL + key
```

`.env` (gitignored) takes the same variables as the other clients:

```
SCOPIO_URL=http://<pi-ip>:8000
SCOPIO_API_KEY=<mint on the Pi with ros2_ws/scripts/generate_api_key.py>
```

The repo's `.mcp.json` registers the server for Claude Code automatically —
start `claude` in the repo root and approve it. If `python` is not the
interpreter that has the requirements, put an absolute path there.

Check it loaded with `/mcp`. Tools appear as `scopio - <name>`.

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

## Safety

`call_service`, `instrument_call` and `galvo_scpi` are unrestricted by design —
they mirror the gateway's generic surface. The ROS nodes currently enforce **no
range limits** on stage, temperature or AWG. Add clamps in the nodes (and/or
scoped API keys in the gateway) before running an agent unattended; a limit
enforced only by a prompt is not a limit.
