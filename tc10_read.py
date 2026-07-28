"""
Minimal connectivity test for the Wavelength Electronics TC10 LAB.

Scans every VISA resource, asks each one *IDN?, picks the TC LAB (ignoring the
Rigol wavegen), then reads the temperature sensor.

    pip install pyvisa pyvisa-py pyusb
    # Linux/Pi also needs libusb + udev access to the USBTMC device:
    #   sudo apt install libusb-1.0-0
    #   (or run with sudo to test quickly)

    python tc10_read.py
"""

import pyvisa

rm = pyvisa.ResourceManager("@py")

tc = None
for res in rm.list_resources():
    try:
        inst = rm.open_resource(res)
        inst.timeout = 3000
        idn = inst.query("*IDN?").strip()
    except Exception as e:
        print(f"{res:<40} -- could not query ({e})")
        continue

    print(f"{res:<40} -- {idn}")

    up = idn.upper()
    if "TC10" in up or "WAVELENGTH" in up:
        tc = inst
    else:
        inst.close()

if tc is None:
    raise SystemExit("No TC10 LAB found. Is it powered on and plugged in?")

print(f"\nUsing: {tc.resource_name}")

units = {"0": "C", "1": "K", "2": "F", "3": "raw"}.get(tc.query("TEC:UNITS?").strip(), "?")
print(f"Sensor      : {tc.query('TEC:SENSOR?').strip()}")
print(f"Setpoint    : {tc.query('TEC:SET?').strip()} {units}")
print(f"Temperature : {tc.query('TEC:ACT?').strip()} {units}")
print(f"TEC output  : {'ON' if tc.query('TEC:OUTput?').strip() == '1' else 'OFF'}")
print(f"Errors      : {tc.query('ERRSTR?').strip()}")

tc.write("LOCAL")   # hand the front panel back
tc.close()
