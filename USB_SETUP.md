# Important setup for external USB instruments

For a fresh raspberry pi the following steps will
need to be followed to install the neccesary drivers
and python libraries to actually see the devices.

## Installations

### Libusb
```
sudo apt update
sudo apt install -y libusb-1.0-0 libusb-1.0-0-dev
```

### Python libraries

```
sudo apt install -y python3-pyvisa python3-pyvisa-py python3-usb
```

## Permissions
First check all of your usb devices with
```lsusb```

I got something like:

```
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 001 Device 002: ID 2109:3431 VIA Labs, Inc. Hub
Bus 001 Device 003: ID 17ef:6099 Lenovo Lenovo Traditional USB Keyboard
Bus 001 Device 004: ID 413c:301a Dell Computer Corp. Dell MS116 Optical Mouse
Bus 001 Device 010: ID 1ab1:0642 Rigol Technologies DG1000Z Serials
Bus 001 Device 011: ID 1a45:3101 Wavelength Electronics TC10-LAB
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
```

My devices are the Rigol Technologies and the Wavelength Electronics one. The important number is the firest 4 numbers after ID on the devices you are connecting.
In my case ```1ab1``` and ```1a45```.

Now you are going to write the following command on the terminal:
```
sudo tee /etc/udev/rules.d/99-usbtmc.rules >/dev/null <<'EOF'
# My first device (CHANGE THE ID IF NEEDED IN YOUR CASE)
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a45", MODE="0666", GROUP="plugdev"
# My second device (CHANGE THE ID IF NEEDED IN YOUR CASE)
SUBSYSTEM=="usb", ATTRS{idVendor}=="1ab1", MODE="0666", GROUP="plugdev"
EOF

sudo udevadm control --reload-rules && sudo udevadm trigger
```
### Final Step
```
groups            # should list plugdev
sudo usermod -aG plugdev $USER    # if not; log out and back in after
```

Now you are done, YAY!!!!

