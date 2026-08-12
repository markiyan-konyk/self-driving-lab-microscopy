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

Want the microscope to run an experiment on its own overnight?
**[TUTORIAL.md](TUTORIAL.md)** walks from here to an unattended campaign.

## Tools

| Tool | What it does |
|---|---|
| `describe_instrument` | Live capability map. Start here — see [Discovery](#discovery). |
| `status` | Health plus latest stage/camera/temperature/AWG/relay/calibration telemetry. |
| `call_service` | Any ROS service by path (`stage/jog`, `calibration/set`, ...). |
| `send_goal` | Long actions: `camera/autofocus`, `stage/move_path`, `scan_region`. |
| `stage_move` | Relative or absolute stage moves, in steps. |
| `camera_controls` | Read or partially write camera settings. |
| `grab_frame` | One frame, downscaled, returned as an image Claude can *see*. |
| `white_balance` | One-shot AWB, then locked. Run it when the light changes. |
| `focus_metric` | Cheap sharpness number for focus sweeps. |
| `record_clip` | Record N seconds to `recordings/<name>/00000.jpg…` + measured fps. |
| `instrument_call` | Any driver method on `galvo` or `temperature`. |
| `laser` | Switch the laser relay, or read it back. |
| `galvo_scpi` | One raw SCPI command to the AWG (`?` ⇒ query). |

## Discovery

Nothing in this server hard-codes what the microscope can do. Twelve of the
thirteen tools are transport; `describe_instrument` is the map, and it is built
from the **live** ROS graph and the **live** driver classes on every call. Add a
node on the Pi or a method to a driver, and an agent can use it the same
minute — no change here, no change to the gateway, no change to the SDK.

It answers in two levels, because the whole map is far too big for one reply
(the two driver classes are ~240 methods, ~7k tokens, before any schemas):

```
describe_instrument()              index: every service, topic and action BY NAME,
                                   plus each instrument and whether it is connected
describe_instrument('stage/jog')   the field schema of that one service
                                   (topics and actions too; either 'stage/jog'
                                   or '/scopio/stage/jog' works)
describe_instrument('galvo')       every driver method on the DG1022Z, with
                                   signature and one-line doc
describe_instrument('galvo.sin')   only the ones matching 'sin'
```

So the usual loop is: index once (~600 tokens), drill into the two or three
things this experiment needs, then work. Instrument methods are introspected
from the **class**, so they list correctly even while that instrument is
unplugged — `connected: false` in the index tells you the difference between
"this capability does not exist" and "that box is switched off".

The server also ships standing `instructions` (shown to the model once at
connect) covering the three rules that are not guessable from a tool schema:
the Pi never analyses, always use `record_clip`'s **measured** fps, and one node
reporting `connected: false` does not mean the microscope is down.

## Where recordings go

`record_clip` writes to `recordings/<name>/` **in the client's working
directory** — the folder you started Claude Code in — never next to this server.
That is what lets the agent read back frames it just recorded with its own file
tools, and it keeps this repo clean when the two live apart. Add `recordings/`
to the working folder's `.gitignore`.

## Test

```bash
pip install fastapi uvicorn websockets       # the mock gateway
python scopio_mcp/test_scopio_mcp.py         # no microscope needed
```

Two checks, no hardware and no Pi:

- **registration** — starts `server.py` as a real MCP client would, over stdio,
  with the microscope deliberately unreachable, and asserts the handshake
  succeeds and the tool set is exactly what this README lists. Registering must
  never depend on the rig being switched on, or a powered-down microscope looks
  like a broken install.
- **behaviour** — calls every tool against the mock gateway from
  `scopio_client`'s test, including that the discovery index stays an index.

Run it after touching `server.py` or bumping the `mcp` SDK. `websockets` is
needed because uvicorn answers WebSocket upgrades with a 404 without it.

## Supported `mcp` versions

Both generations: 1.x (`mcp.server.fastmcp.FastMCP`) and 2.x
(`mcp.server.mcpserver.MCPServer`). `server.py` imports whichever is installed —
the decorator, `run()` and `Image` surface it uses is identical across the
rename, so you never have to match the SDK to the server.

## Safety

`call_service`, `instrument_call` and `galvo_scpi` are unrestricted by design —
they mirror the gateway's generic surface. `laser` is a separate tool for one
reason: an allowlist (see [TUTORIAL.md](TUTORIAL.md) step 5) can then permit or
withhold the laser on its own, which it cannot do for a capability that is only
reachable through a generic escape hatch.

The ROS nodes enforce **no range limits** on stage, temperature or AWG. Add
clamps in the nodes (and/or scoped API keys in the gateway) before running an
agent unattended; a limit enforced only by a prompt is not a limit.
