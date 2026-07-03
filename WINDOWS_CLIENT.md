# Running the SCOPIO clients from Windows (via WSL2)

Goal: run the **UI** and **galvo_draw** containers on a **Windows laptop**, talking
to the **backend on the Pi** over ROS 2 / DDS. No new hardware; the Pi only runs the
drivers (it's weak and low on disk). Once a client is up, anyone (Windows/Mac/phone)
just opens a browser to it — only the machine running the container needs this setup.

---

## Read this first — why NOT plain Docker Desktop

We tried it. **Docker Desktop on Windows cannot do this**, and it's not a config you
can fix. The Docker *engine* runs inside Docker Desktop's own hidden VM, which is
NAT'd and does **not** see the Windows LAN — so the container gets an internal
`192.168.65.x` address and can never reach the Pi, even with "host networking" and
WSL "mirrored" mode both enabled. This is documented by Docker themselves; their
prescribed fix is: **run Docker Engine (docker-ce) directly inside a WSL2 Ubuntu
distro.** ROS 2 discovery (DDS) needs a real host network identity, which only the
WSL2-native engine provides.

So the working method is: **WSL2 Ubuntu + docker-ce**, not Docker Desktop.
You still just run `docker compose up` — the difference is *where* the engine lives.

Refs: Docker Forums "Host networking not working on Docker Desktop in WSL2 with
mirrored mode" and "How to access a Linux container on a LAN network".

---

## Requirements
- **Windows 11 22H2+** (build ≥ 22621). Mirrored networking doesn't exist on Win 10.
- The **direct ethernet cable** to the Pi (most reliable; dodges lab-WiFi multicast
  blocking). Laptop uses its USB-ethernet adapter; Pi uses its RJ45 port.

---

## One-time setup — Windows host

1. **Enable WSL2 mirrored networking.** Edit `%UserProfile%\.wslconfig` (create it if
   missing) so it contains **exactly** this (no stray lines — a leftover ``` ```ini ```
   fence will break it):
   ```ini
   [wsl2]
   networkingMode=mirrored
   dnsTunneling=true
   firewall=true
   ```
   Then apply it: `wsl --shutdown` in PowerShell.

2. **Install an Ubuntu WSL2 distro** (if you don't have one):
   ```powershell
   wsl --install -d Ubuntu-24.04
   ```
   Create a username/password when prompted; it drops you into an Ubuntu shell.

3. **Allow the DDS UDP ports through Windows Firewall** (mirrored mode enforces it).
   In an **admin** PowerShell:
   ```powershell
   New-NetFirewallRule -DisplayName "ROS2 DDS" -Direction Inbound -Protocol UDP -LocalPort 7400-7600 -Action Allow
   ```

4. **Set the laptop's cable IP.** Plug the cable in first (the adapter appears as e.g.
   "Ethernet 2"). Admin PowerShell:
   ```powershell
   Get-NetAdapter                       # find the USB-ethernet adapter's name/status (must be Up)
   New-NetIPAddress -InterfaceAlias "Ethernet 2" -IPAddress 10.42.0.2 -PrefixLength 24
   ```
   (swap `"Ethernet 2"` for your adapter's name).

## One-time setup — Raspberry Pi

Set eth0 to a static IP through NetworkManager so it **persists across reboots** (a
plain `ip addr add` gets silently flushed by NetworkManager on Pi OS Bookworm):
```bash
sudo nmcli con add type ethernet con-name pi-direct ifname eth0 ipv4.method manual ipv4.addresses 10.42.0.1/24
sudo nmcli con up pi-direct
ip -br addr        # confirm eth0 now shows 10.42.0.1/24
```
(If the wired NIC is named `end0`, use that — check `ip -br link`.)

## Verify the cable

From the Ubuntu WSL2 shell (thanks to mirrored networking it sees the cable directly):
```bash
hostname -I | grep 10.42.0.2      # the cable IP shows here
ping -c3 10.42.0.1                # reaches the Pi
```
Both must pass before continuing. If `10.42.0.2` is missing here, mirrored networking
didn't take effect — recheck step 1 and run `wsl --shutdown` again.

---

## One-time setup — Docker inside WSL2

> First, stop Docker Desktop from hijacking `docker` in this distro: **quit Docker
> Desktop** from the Windows tray, or Settings → Resources → WSL Integration → turn
> **off** the toggle for Ubuntu-24.04. Otherwise `docker` here talks to Docker
> Desktop's broken NAT engine.

Install native Docker Engine in the Ubuntu shell:
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
sudo service docker start
newgrp docker
docker ps          # empty list = the WSL-native engine is running
```
(After a WSL restart, if `docker ps` says "cannot connect to the daemon", just run
`sudo service docker start` again.)

## Get the repo into the Linux filesystem

Building from `/mnt/c` is slow and hits permission quirks — copy it in:
```bash
cp -r /mnt/c/MatterLab/Self_Driving/self-driving-lab-microscopy ~/scopio
```
(Adjust the source path to wherever the repo lives on your Windows drive.)

---

## Run it

**Backend on the Pi** first:
```bash
cd ~/self-driving-lab/ros2_ws
docker compose up            # drivers only
```
**Clients in the Ubuntu WSL2 shell** (the `docker-compose.override.yml` files
auto-apply `dds/client_peers.xml`, pointing discovery straight at the Pi):
```bash
cd ~/scopio/ui         && docker compose up --build    # -> http://localhost:8080
cd ~/scopio/galvo_draw && docker compose up --build    # -> http://localhost:8090
```
`ROS_DOMAIN_ID` must match on both ends (default 0). Open the URLs in any Windows
browser — localhost forwards from WSL2 automatically.

### One-command launch from a Windows terminal (optional)
You can trigger it from PowerShell without opening a WSL shell — it still runs on the
WSL engine under the hood:
```powershell
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/scopio/ui && docker compose up --build"
```

## Verify discovery
In a second Ubuntu tab:
```bash
docker exec -it scopio-ui bash -lc "hostname -I"
# must include 10.42.0.2  (proves the container is on the cable, not Docker NAT)

docker exec -it scopio-ui bash -lc \
  "source /opt/ros/jazzy/setup.bash && source /iface_ws/install/setup.bash && ros2 topic list"
# expect: /scopio/camera/state, /scopio/image/compressed, /scopio/stage/position, ...
```
Topics appear → open the browser, you're done.

## Camera feed

The camera can't run inside the Ubuntu ROS container (picamera2/libcamera would
have to match the Pi kernel — fragile). So the live view comes from a tiny native
MJPEG server on the Pi, which the UI ingests. Stage/galvo control still go over ROS.

On the **Pi host** (not in Docker — picamera2 is native to Raspberry Pi OS):
```bash
cd ~/self-driving-lab
python3 pi_camera_server.py            # serves http://<pi>:8081/stream.mjpg
```
The UI override (`ui/docker-compose.override.yml`) already sets
`CAMERA_MJPEG_URL=http://10.42.0.1:8081/stream.mjpg`, so after the UI container
starts it pulls that stream into the normal `/video_feed` (live view **and**
recording work). If you reach the Pi at a different IP, change that URL.

> This is a pragmatic bridge so the camera works today. The "proper" path —
> camera frames published on the ROS graph — is a follow-up. The camera-setting
> sliders (framerate/gains) still target the ROS camera node, so they won't affect
> this MJPEG feed yet.

## Troubleshooting
- **Container `hostname -I` shows only `192.168.65.x` / `172.x`** → you're still on
  Docker Desktop's engine. Quit Docker Desktop / disable its WSL integration, and make
  sure you installed docker-ce (step above) and are running commands in the **Ubuntu**
  shell.
- **`10.42.0.2` missing from the WSL shell's `hostname -I`** → mirrored networking
  isn't active. Fix `.wslconfig` (step 1), `wsl --shutdown`, reopen Ubuntu.
- **Cable `ping 10.42.0.1` fails** → the Pi's static IP didn't stick. Re-run the Pi
  `nmcli` commands; confirm with `ip -br addr` that eth0 shows `10.42.0.1/24`. Check
  the adapter is **Up** in `Get-NetAdapter`.
- **Ping works but no `/scopio/...` topics** → confirm the backend is actually up on
  the Pi (`docker exec -it scopio bash -lc "source /opt/ros/jazzy/setup.bash && source
  /ros2_ws/install/setup.bash && ros2 topic list"` should list them there). As a
  belt-and-braces measure, also force the Pi to reply unicast: export
  `FASTRTPS_DEFAULT_PROFILES_FILE=$PWD/../dds/pi_peers.xml` before `docker compose up`
  in `ros2_ws`.
