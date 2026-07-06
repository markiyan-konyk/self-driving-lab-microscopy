# SCOPIO — Full setup: run the microscope from a Windows laptop

This is the complete, from-zero guide to controlling the SCOPIO microscope from a
**Windows laptop**, with the **Raspberry Pi** doing the hardware driving. Follow it
top to bottom the first time; most steps are one-time.

### What runs where

| Machine | Runs | How |
|---|---|---|
| **Raspberry Pi** | ROS 2 backend (stage, galvo/AWG, calibration) | Docker (`ros2_ws`) |
| **Raspberry Pi** | Camera stream + camera controls | native Python (`pi_camera_server.py`) |
| **Windows laptop** | UI + galvo_draw web apps | Docker inside **WSL2 Ubuntu** |
| **Any device** | Just viewing/using the UI | a web browser → the laptop |

The laptop apps are **ROS 2 clients**: they discover the Pi's backend over the
network and drive it. The camera is served separately over HTTP (see Part 1.4).
Once the laptop's UI is up, teammates only need a browser — nothing else.

### Why not Docker Desktop (read once)
Docker Desktop on Windows runs containers inside its own NAT'd VM that can't reach
your LAN, so a container can never find the Pi over ROS 2's DDS discovery — this is
a documented Docker limitation, not a setting. The working method (Docker's own
prescribed fix) is **Docker Engine installed directly inside a WSL2 Ubuntu distro**,
which gets a real network identity. You do **not** need Docker Desktop at all.

---

# Part 1 — Raspberry Pi (backend + camera)

Do the Pi first: the laptop apps look for it on startup.

### 1.1 Get the code
```bash
cd ~
git clone https://github.com/markiyan-konyk/self-driving-lab-microscopy.git self-driving-lab   # first time
# or, if already cloned:
cd ~/self-driving-lab && git checkout ros2-bigchange && git pull
```

### 1.2 Pin the galvo AWG address
The backend talks to the Rigol AWG over USB. Find its VISA address and pin it, so the
node opens it directly instead of scanning (scanning is slow and unreliable inside the
container, and intermittently fails to find the device):
```bash
cd ~/self-driving-lab/ros2_ws
# find the address (needs the container built once; or run on the host where galvo_tests work):
python3 -c "import pyvisa; print(pyvisa.ResourceManager().list_resources())"
# take the USB0::... entry and write it to .env (Compose reads this automatically):
echo 'GALVO_RESOURCE=USB0::6833::1602::DG1ZA278M01038::0::INSTR' > .env   # your real string
```

### 1.3 Start the ROS backend
```bash
cd ~/self-driving-lab/ros2_ws
sudo docker compose up --build          # first time builds the image; later runs: just `up` without the build
```
Leave it running. The image already includes `libusb-1.0-0`, which pyvisa needs to see
the USB AWG — without it the galvo reports "AWG unavailable".

Verify the backend is alive and connected to the AWG (in another Pi terminal):
```bash
sudo docker exec -it scopio bash -lc "source /opt/ros/jazzy/setup.bash && source /ros2_ws/install/setup.bash && ros2 topic list"
# expect /scopio/stage/position, /scopio/awg/status, /scopio/calibration, ...
sudo docker exec -it scopio bash -lc "source /opt/ros/jazzy/setup.bash && source /ros2_ws/install/setup.bash && ros2 topic echo /scopio/awg/status --once"
# want: connected: true, idn: Rigol Technologies,DG1022Z,...
```

### 1.4 Start the camera server
The Pi camera can't run inside the Ubuntu container (its libcamera stack must match the
Pi kernel), so it's served natively from the host — this also gives real camera controls
(exposure, gains, white balance) that the UI drives over HTTP:
```bash
cd ~/self-driving-lab
python3 pi_camera_server.py              # serves http://<pi>:8081  (stream + /controls)
```
Leave it running (open a second SSH session, or use `tmux`). If picamera2 is missing:
`sudo apt install -y python3-picamera2`.

### 1.5 Give the Pi a static IP on the cable
Set the wired port to a fixed address via NetworkManager so it **survives reboots** — a
plain `ip addr add` is silently wiped by NetworkManager on Pi OS Bookworm:
```bash
sudo nmcli con add type ethernet con-name pi-direct ifname eth0 ipv4.method manual ipv4.addresses 10.42.0.1/24
sudo nmcli con up pi-direct
ip -br addr        # confirm eth0 shows 10.42.0.1/24  (if your NIC is 'end0', use that name)
```
This adds a private point-to-point address with no gateway, so it won't disturb the
Pi's WiFi internet.

---

# Part 2 — Connect the ethernet cable

Plug a cable between the **Pi's RJ45 port** and the **laptop's USB-ethernet adapter**.

Why a direct cable (not WiFi): it's a private link with no router in between, so ROS 2
discovery is rock-solid and it dodges lab-WiFi client isolation / multicast blocking.
It also works for any teammate — no VPN accounts needed.

---

# Part 3 — Windows laptop (the UI client)

### 3.1 Requirement
**Windows 11, version 22H2 or newer** (build ≥ 22621). Check with `winver`. The mirrored
networking we rely on does not exist on Windows 10 — on Win 10 you'd need a different
machine or the Ubuntu side of a dual-boot.

### 3.2 Install WSL2 Ubuntu
In PowerShell:
```powershell
wsl --install -d Ubuntu-24.04
```
Set a username/password when prompted; it drops you into an Ubuntu shell. (If WSL was
already installed, `wsl --update` to be current.)

### 3.3 Turn on mirrored networking
This makes the WSL Ubuntu (and its Docker containers) share the laptop's **real** network
interfaces instead of a private NAT — that's what lets a container reach the Pi over the
cable. Create/edit `%UserProfile%\.wslconfig` so it contains **exactly**:
```ini
[wsl2]
networkingMode=mirrored
dnsTunneling=true
firewall=true
```
(Don't leave a stray ```` ```ini ```` code-fence line in the file — WSL will ignore the
setting.) Apply it:
```powershell
wsl --shutdown
```
Reopen the Ubuntu shell afterwards.

### 3.4 Allow the DDS ports through Windows Firewall
Mirrored mode enforces the Windows firewall, so open the ROS 2 discovery ports. In an
**admin** PowerShell:
```powershell
New-NetFirewallRule -DisplayName "ROS2 DDS" -Direction Inbound -Protocol UDP -LocalPort 7400-7600 -Action Allow
```

### 3.5 Give the laptop a static IP on the cable
Plug the cable in first, then in **admin** PowerShell:
```powershell
Get-NetAdapter                        # find the USB-ethernet adapter (must show Status = Up)
New-NetIPAddress -InterfaceAlias "Ethernet 2" -IPAddress 10.42.0.2 -PrefixLength 24
```
Swap `"Ethernet 2"` for your adapter's actual name (in my computer it was actually called like that)

### 3.6 Verify the cable from inside WSL
In the Ubuntu shell (mirrored networking makes it see the cable directly):
```bash
hostname -I | grep 10.42.0.2      # the cable IP appears here  → mirrored networking works
ping -c3 10.42.0.1                # reaches the Pi
```
Both must pass before continuing. If `10.42.0.2` is missing, mirrored mode didn't take —
recheck step 3.3 and `wsl --shutdown` again. If the ping fails, recheck the Pi's IP (1.5)
and that the adapter is `Up`.

### 3.7 Install Docker Engine inside WSL
> If Docker Desktop is installed, first stop it from hijacking `docker` here: **quit
> Docker Desktop**, or Settings → Resources → WSL Integration → turn **off** Ubuntu-24.04.
> Otherwise `docker` in this shell silently uses Docker Desktop's unreachable engine.

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
sudo service docker start
newgrp docker
docker ps          # empty table = the WSL-native engine is running
```
(After a Windows/WSL restart, if `docker ps` says "cannot connect to the daemon", run
`sudo service docker start` again.)

### 3.8 Get the code into WSL
Build from the Linux filesystem (building from `/mnt/c` is slow and hits permission
quirks). Clone fresh so you're never on a stale copy:
```bash
git clone https://github.com/markiyan-konyk/self-driving-lab-microscopy.git ~/scopio
cd ~/scopio && git checkout ros2-bigchange
```

### 3.9 Run the apps
The repo ships the network config already wired in (`dds/client_peers.xml` points
discovery straight at `10.42.0.1`, and the UI's override sets the camera URL), so it's
just:
```bash
cd ~/scopio/ui         && docker compose up --build    # → http://localhost:8080
```
And in another Ubuntu tab, the laser drawing app:
```bash
cd ~/scopio/galvo_draw && docker compose up --build    # → http://localhost:8090
```
Open the URLs in any Windows browser (localhost forwards from WSL automatically). The
default UI password is `password` (override with `SCOPIO_UI_PASSWORD`).

### 3.10 One-command launch (optional)
Once it works, teammates can start it from plain PowerShell without opening WSL:
```powershell
wsl -d Ubuntu-24.04 -- bash -lc "cd ~/scopio/ui && docker compose up"
```

---

# Part 4 — Verify end to end

In a second Ubuntu tab:
```bash
# 1. Is the container on the cable (not Docker NAT)?
docker exec -it scopio-ui bash -lc "hostname -I"          # must include 10.42.0.2

# 2. Does it see the Pi's ROS graph?
docker exec -it scopio-ui bash -lc \
  "source /opt/ros/jazzy/setup.bash && source /iface_ws/install/setup.bash && ros2 topic list"
# expect /scopio/stage/position, /scopio/awg/status, ...
```
Then in the browser (`http://localhost:8080`):
- **Live video** appears (from the Pi camera server).
- **Stage arrows** move the microscope; **camera sliders / white balance** change the
  image; **autofocus** sweeps Z and returns to the sharpest plane (you'll hear the motor).
- In galvo_draw (`http://localhost:8090`), **Test link** traces a circle with the laser.

---

# Everyday startup (after first-time setup)

1. **Pi**: `cd ~/self-driving-lab/ros2_ws && sudo docker compose up` — then, in another
   session, `python3 ~/self-driving-lab/pi_camera_server.py`.
2. **Laptop** (WSL Ubuntu): `sudo service docker start` if needed, then
   `cd ~/scopio/ui && docker compose up`.
3. Browser → `http://localhost:8080`.

---

# Troubleshooting

**Container `hostname -I` shows only `192.168.65.x` / `172.x`, no `10.42.0.2`**
You're on Docker Desktop's engine, or mirrored networking is off. Quit Docker Desktop /
disable its WSL integration, confirm docker-ce is installed (3.7), and that `.wslconfig`
has `networkingMode=mirrored` with a `wsl --shutdown` after.

**`10.42.0.2` missing from the WSL shell's own `hostname -I`**
Mirrored networking didn't apply. Fix `.wslconfig` (3.3), `wsl --shutdown`, reopen.

**`ping 10.42.0.1` fails**
Pi IP didn't stick — re-run the `nmcli` commands (1.5), confirm `ip -br addr` shows
`10.42.0.1/24`; on Windows confirm the adapter is `Up` (`Get-NetAdapter`).

**Ping works but `ros2 topic list` shows no `/scopio/...`**
Confirm the backend is up on the Pi (Part 1.3 check). If topics show on the Pi but not in
the container, force the Pi to reply unicast too: on the Pi, before `docker compose up`,
`export FASTRTPS_DEFAULT_PROFILES_FILE=$PWD/../dds/pi_peers.xml`.

**Camera controls / white balance / autofocus do nothing (but stage works)**
The UI isn't reaching the Pi camera server. Check it's set and reachable:
```bash
docker exec scopio-ui bash -lc 'echo "$CAMERA_MJPEG_URL"; python3 -c "import urllib.request;print(urllib.request.urlopen(\"http://10.42.0.1:8081/controls\",timeout=5).read())"'
```
Empty URL → you built from a stale copy; rebuild from `~/scopio/ui`. 404/timeout → the Pi
camera server isn't the current version or isn't running (restart `pi_camera_server.py`).
Autofocus also needs the **stage connected** on the Pi (it moves Z).

**galvo_draw says "AWG unavailable"**
The backend didn't open the AWG. On the Pi: `ros2 topic echo /scopio/awg/status --once`
should show `connected: true`. If not: confirm the container lists the USB device
(`docker exec scopio bash -lc 'python3 -c "import pyvisa; print(pyvisa.ResourceManager().list_resources())"'`),
that `.env` has the exact `GALVO_RESOURCE` string (1.2), and if it worked once then
stopped, **power-cycle the AWG** (unplug USB + power, replug) to clear a stuck USB claim,
then `sudo docker compose restart scopio`.

**`docker compose up` errors with "no space left on device" on the Pi**
Reclaim Docker space: `sudo docker builder prune -af`, `sudo docker image prune -f`,
and remove any client images that shouldn't be on the Pi
(`sudo docker image rm scopio-ui:jazzy scopio-galvo-draw:jazzy`). Also
`sudo apt-get clean` and `sudo journalctl --vacuum-size=50M`.
