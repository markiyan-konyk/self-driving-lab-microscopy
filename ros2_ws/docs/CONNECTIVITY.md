# Connecting to SCOPIO

**External programs connect through the API gateway** — HTTP/WebSocket on
port 8000 with an API key. That is the supported, documented, authenticated
path, and it works from any OS on any network that can reach the Pi:

- Manual: [`../../docs/API.md`](../../docs/API.md)
- SDK: [`../../scopio_client`](../../scopio_client)
- Interactive docs: `http://<pi>:8000/docs`
- Discovery: `GET /api/v1/interfaces`

```bash
curl http://<pi>:8000/api/v1/health
curl -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"command": ":OUTPut1 OFF"}' \
     http://<pi>:8000/api/v1/service/awg/write
```

Nothing below this line is needed to *use* the microscope. The rest of this
doc is about the raw DDS layer, which is now an implementation detail.

## The DDS graph (on-Pi / backend development only)

Inside the Pi, the driver nodes and the gateway share one host-network DDS
graph (`ROS_DOMAIN_ID` 0, Fast-DDS). Because everything that needs the graph
runs on the same machine, discovery is trivially localhost — none of the old
cross-machine DDS ceremony (unicast peer XMLs, UDP 7400-7600 firewall holes,
WSL2 mirrored networking) exists anymore.

For backend development you can still poke the graph directly *on the Pi*:

```bash
docker exec -it scopio bash
ros2 topic list                              # /scopio/...
ros2 topic echo /scopio/stage/position
ros2 service call /scopio/stage/jog scopio_interfaces/srv/StageJog "{dx: 40}"
```

A second machine with ROS 2 on the same LAN *can* still join the graph (same
domain ID, multicast permitting) — occasionally handy for Foxglove or
debugging — but it is unauthenticated and unsupported as a client path. If
you find yourself wiring DDS peers files again, stop: add what you need to
the gateway instead.

## Off-site access (when it comes up)

The gateway is ordinary HTTP, so remote access is ordinary web plumbing:
put the Pi on a mesh VPN (Tailscale is the easy one) and use
`http://<tailscale-ip>:8000` exactly as on the LAN, or front it with a TLS
reverse proxy. Do **not** port-forward it raw to the internet — the API key
would travel unencrypted.

## Security model

- The **gateway is the only LAN-facing surface** (port 8000): API-key auth on
  every route except `/api/v1/health`, keys in `ros2_ws/secrets/api_keys.json`
  (generate/revoke with `scripts/generate_api_key.py`; hot-reloaded).
- The **camera server binds to loopback** (127.0.0.1:8081) — reachable only
  through the gateway's authenticated proxy.
- The **DDS graph has no auth** — which is acceptable precisely because it no
  longer needs to face the network; it's localhost plumbing between the
  drivers and the gateway. Keep it that way.
