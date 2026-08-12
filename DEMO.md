# Running the SCOPIO demo

Three acts: bring the microscope up, drive it from the UI, then hand it to an
agent. Roughly 15 minutes cold, 2 minutes if the Pi is already running.

The story to tell while it runs: **the Pi is the sensor and the effectuator;
everything that thinks is a client.** The UI and the agent are the same kind of
thing — two API clients of one authenticated gateway, with equal access.

---

## Act 0 — the Pi (once, before anyone is watching)

```bash
ssh <pi>
cd self-driving-lab-microscopy/ros2_ws
cp .env.example .env                        # instrument addresses, if you name them
python3 scripts/generate_api_key.py demo    # prints the key -- keep it
docker compose up -d --build
curl http://127.0.0.1:8000/api/v1/health
```

**`--build` is not optional.** The nodes run from the image, not from the
mounted repo, so `docker compose up -d` alone silently reruns the old code. The
launch log prints the image's build stamp as its first line — if that predates
your last edit, you are watching the old binary. This bites hardest right now:
the galvo driver fix in this pass lives in the image.

Healthy looks like `{"ok":true,"ros_ok":true,"camera_ok":true,"auth_configured":true}`.

```bash
docker compose logs scopio | grep -E "connected|unavailable"
```

Each node says which hardware it found. A node reporting `unavailable` is
fine — the graph still comes up and everything else works. A node **missing
from `ros2 node list` entirely** is the one failure that is never hardware; it
means an import crashed.

## Act 1 — the UI

On your laptop, not the Pi:

```bash
pip install -r ui/requirements.txt
# ui/.env:  SCOPIO_URL=http://<pi-ip>:8000
#           SCOPIO_API_KEY=<the key from Act 0>
python ui/run_ui.py                         # -> http://localhost:8080
```

Worth showing, in this order:

1. **Live video and the HUD** — X/Y/Z and the measured frame rate.
2. **Jog the stage** with the arrow pad or the arrow keys. Every press is
   `POST /api/v1/service/stage/jog` through the gateway into ROS and out to the
   Sangaboard.
3. **Set Scale → Measure** — click two points on a known distance, then measure
   anything. The scale is stored in `calibration_node` on a *latched* topic, so
   a client that connects ten minutes later still gets it immediately.
4. **The laser switch** (top left, amber). It is the only amber control on the
   page on purpose. Note what it does when the relay is unreachable: it shows
   the last known state and says "assume LIVE" — unknown is never reported as
   off.
5. **Galvo X/Y** — move both sliders, then press **Update Galvo**. Nothing
   reaches the wavegen until you press it; the DG1022Z's command buffer is small
   and streaming slider drags overruns it.
6. **Temperature** — type a setpoint, press Enable. Two commands, exactly like
   the instrument: the number only stores a target, the TEC drives nothing until
   the output is on. The reading's colour is the whole status display.
7. **Record** — pick the folder in **Save Clips To** first. The clip is written
   on the machine running the UI, one written frame per captured frame. The Pi
   never records: it has 4 GB and a camera to feed.

Open a second UI from another laptop against the same key to make the point that
nobody owns the rig.

## Act 2 — the agent

Same microscope, same gateway, same key — a different client.

```bash
pip install -e ./scopio_client -r scopio_mcp/requirements.txt
cp scopio_mcp/.env.example scopio_mcp/.env      # same URL and key as the UI
claude                                          # approve the `scopio` server
```

`/mcp` lists the tools. Then, in order:

> Call `describe_instrument`. What can this microscope do?

It gets an index — every service, topic and action by name, and the two
instruments. Then ask for detail on one thing:

> Now `describe_instrument('galvo')`. How would you drive the tweezers?

That list is introspected from the live driver class, not written into the MCP
server. **Add a method to `dg1022z.py` in the morning and the agent can call it
in the afternoon** — no new endpoint, no gateway change, no SDK release.

> Grab a frame and tell me what you see. Is it in focus?

`grab_frame` returns the actual JPEG, so the model *looks* at the sample rather
than reading a number about it. Follow with a stage move and another frame to
show it closing the loop.

> Record a 10 second clip, then write a script that estimates the diffusion
> coefficient of the beads.

This is the whole thesis in one command: the Pi streams, the client thinks. The
frames land in the agent's own working directory and it analyses them with code
it writes itself. `scopio_mcp/TUTORIAL.md` takes this to an unattended
overnight campaign.

---

## If something is wrong

| Symptom | Look here |
|---|---|
| UI says "Offline" | `curl http://<pi>:8000/api/v1/health` from the laptop. `auth_configured:false` = no key file; a refused connection = network or the container is down. |
| Video frozen, not disconnected | `curl localhost:8080/health` on the laptop: compare `frames_ingested` with the camera server's own `frames` (`curl localhost:8081/controls` on the Pi). Both climbing = the break is in the browser. |
| Galvo offline | `docker compose logs scopio \| grep AWG`. The log echoes the resource string it tried: a *parsing* error is a wrong `GALVO_RESOURCE`, *not found* is a cable. |
| Temperature flapping | It takes three consecutive failures to drop the session, so flapping means real timeouts. Lower `publish_rate` in `params.yaml` — `status()` costs five USB-TMC round trips per poll. |
| Laser button disabled | `relay_node` could not claim the pin; `relay/set` now returns the reason. Check `GPIOZERO_PIN_FACTORY` and that `/dev/gpiochip*` is visible inside the container. |
| An instrument vanished after replugging | `privileged` snapshots `/dev` at container creation. The compose file bind-mounts `/dev` to avoid this; if you removed that, recreate the container. |

Before a demo, the five-minute confidence check — no hardware needed:

```bash
python ros2_ws/scripts/test_drivers.py
python scopio_client/test_scopio_client.py
python scopio_mcp/test_scopio_mcp.py
python ui/test_run_ui.py
```
