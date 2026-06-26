# Connecting to the SCOPIO ROS graph (now and later)

The whole point of the rewrite is that **any program can drive the rig** by
joining the ROS 2 graph and speaking `scopio_interfaces` (see `INTERFACES.md`).
This doc explains how a program connects today and the path to running it off
the Pi later.

## How ROS 2 discovery works (the one thing to understand)
ROS 2 uses DDS. Nodes on the **same network** with the **same `ROS_DOMAIN_ID`**
(default `0`) find each other **automatically** — no IP addresses, no broker.
The container runs with `network_mode: host`, so the Pi's nodes are on the LAN
directly. That is why a second computer "just sees" `/scopio/...`.

```bash
# on any machine with ROS 2 installed + sourced, same LAN, same domain:
export ROS_DOMAIN_ID=0
ros2 topic list            # should show /scopio/...
ros2 topic echo /scopio/camera/state
```
If you run several rigs on one LAN, give each a distinct `ROS_DOMAIN_ID` so they
don't cross-talk.

## Now: same-LAN clients (works today)
- The UI (`ui_gateway`) runs on the Pi and is reached at `http://<pi-ip>:8080`.
- Any **other** program (a galvo app, a controller) runs on any LAN machine,
  joins the graph, and calls services / subscribes to topics. Nothing special is
  required — this is the intended way to build side-apps now.
- Verify external control end-to-end:
  ```bash
  ros2 service call /scopio/awg/write scopio_interfaces/srv/AwgWrite \
    "{command: ':OUTPut1 OFF'}"
  ```

## Now: over a USB cable (point-to-point, no network)
Put the Pi in **USB-Ethernet gadget** mode so a laptop connected by USB shares a
private link the DDS traffic rides on:
1. Enable the gadget (`dtoverlay=dwc2`, `modules-load=dwc2,g_ether` on the Pi).
2. The cable then presents a `usb0` network interface on both ends with
   link-local IPs.
3. With ROS 2 sourced and the same `ROS_DOMAIN_ID` on both, discovery works over
   that interface exactly like a LAN. (If multicast is flaky on the link, set a
   unicast `ROS_STATIC_PEERS` / a Fast DDS peers file — documented when needed.)

This is the planned "connect over USB" path; only the link changes, not the ROS
interfaces.

## Later: off-site / cloud / cluster (documented, not yet implemented)
DDS multicast does not cross the open internet. Two clean options when we get
there:
- **Mesh VPN** (Tailscale / Husarnet / ZeroTier): puts the remote machine on the
  same *virtual* LAN, so DDS "just works" with no ROS changes. Simplest.
- **`zenoh-bridge-ros2dds`**: a bridge process on each side that carries the ROS
  graph over a single TCP/QUIC connection — better for lossy/WAN links and
  firewalls.

Either way the **interfaces do not change** — that is the payoff of freezing the
contract. A cloud controller is just another client.

## Security note
There is currently **no auth on the ROS graph** — anyone on the network/domain
can command the hardware. Keep it on a trusted LAN/VPN. The web UI keeps its own
password; the raw ROS layer does not. Add DDS Security (SROS2) or keep it behind
the VPN before exposing it more widely.
