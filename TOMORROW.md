# Tomorrow: bring the microscope online

## 1. On the Pi

```bash
cd ~/self-driving-lab-microscopy && git checkout remake && git pull

# if the galvo/AWG is attached, pin its address (find it: python3 -m pyvisa info)
echo 'GALVO_RESOURCE=USB0::...' > ros2_ws/.env

cd ros2_ws
python3 scripts/generate_api_key.py laptop      # COPY the printed key
docker compose up -d --build                    # first build takes a while

curl http://127.0.0.1:8000/api/v1/health         # expect ros_ok: true
curl http://127.0.0.1:8081/controls              # camera server alive?
hostname -I                                      # NOTE this IP
```
If `camera_ok` is false: `docker logs scopio-camera`, then see
`camera_server/README.md` for the systemd fallback.

## 2. Connect Pi + laptop over WiFi

Join **both** to the same network, then from the laptop:
`curl http://<pi-ip>:8000/api/v1/health`

⚠️ **Most likely failure: school/lab WiFi blocks device-to-device traffic**
("client/AP isolation") even though both devices are online. Symptom: the
curl above hangs/times out, but the *same* curl run locally on the Pi works
fine. Fixes, cheapest first:
1. Ask IT for a non-isolated "IoT"/lab-devices SSID.
2. Use a phone hotspot or a travel router instead of the school AP.
3. Direct ethernet cable Pi↔laptop with static IPs (`docs/WINDOWS_CLIENT.md`
   has this as a fallback).

## 3. On the laptop

```powershell
git pull   # (or clone), git checkout remake
pip install -r ui\requirements.txt

$env:SCOPIO_URL     = "http://<pi-ip>:8000"
$env:SCOPIO_API_KEY = "<key from step 1>"

curl $env:SCOPIO_URL/api/v1/health   # sanity check

python ui\run_ui.py                  # -> http://localhost:8080
```

Full detail / troubleshooting: `docs/BRINGUP.md` (Pi), `docs/WINDOWS_CLIENT.md`
(laptop), `docs/API.md` (every command).
