import time
import threading
import pyvisa
import os
import numpy as np

try:
    import readchar
except ImportError:            # interactive-only (used by _calibrate_offset); may be absent
    readchar = None

class DG1022Z:
    def __init__(self, resource="", timeout_ms=5000, backoff_s=0.5):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._backoff_s = backoff_s
        self._lock = threading.RLock()   # VISA sessions are NOT thread-safe
        self.reconnects = 0
        self.rm = None
        self.device = None

        self.xpos = 0.0
        self.ypos = 0.0
        self.xoffset = 0.0
        self.yoffset = 0.0
        self.freq = 10.0
        self.amp = 0.0
        self.phase = 0.0


    def _open(self):
        self.rm = pyvisa.ResourceManager("@py")
        env = os.environ.get("DAC_ID")
        if env:
            self.resource = env
        if not self.resource:
            resources = self.rm.list_resources('USB?*INSTR')
            if not resources:
                raise RuntimeError("No USB VISA instruments found. Check physical connection.")
            self.resource = resources[0]
            self.device = self.rm.open_resource(self.resource)
        else:
            self.device = self.rm.open_resource(self.resource)
        self.device.read_termination = "\n"
        self.device.write_termination = "\n"
        self.device.timeout = self.timeout_ms

    def _open_debug(self):
        self.rm = pyvisa.ResourceManager("@py")
        env = os.environ.get("DAC_ID")
        if env:
            self.resource = env
        else:
            print("No os.environ input detected")
        
        if not self.resource:
            resources = self.rm.list_resources()
            print("ALL VISA resources:", resources or "(none found)")
            resources = self.rm.list_resources('USB?*INSTR')
            print("VISA resources starting with USB:", resources or "(none found)")
            if not resources:
                raise RuntimeError("No USB VISA instruments found. Check physical connection.")
            print(type(resources[0]))
            print(f"Using the device with ID:{resources[0]}")
            self.device= self.rm.open_resource(resources[0])
            print(f"Using the device with ID:{resources[0]}")
        else:
            print(f"Using the device with ID:{resources[0]}")
            self.device = self.rm.open_resource(self.resource)
            print(f"Using the device with ID:{self.resource}")

        self.device.read_termination = "\n"
        self.device.write_termination = "\n"
        self.device.timeout = self.timeout_ms
        print(f"Timeout set to {self.timeout_ms}")
        id = self.device.query("*IDN?").strip()
        print(f"IDN:{id}")
        if "DG1" not in id.upper():
            print("WARNING: this does not look like a DG1022Z. Double-check the "
                  "resource address.")
        print("OK - connection works.")

    def _close(self):
        self.device.write(":OUTP1 OFF;:OUTP2 OFF")
        self.device.close()

    def _calibrate_offset(self):
        self.dcinit()
        print("Calibrate the X axis")
        print(f"Starting at {self.xoffset}")
        print("Controls: [UP/DOWN] Change number | [s] Save\n")
        while True:
            key = readchar.readkey()

            if key == readchar.key.UP:
                self.xoffset += 0.01
                print(f"Current offset: {self.xoffset}    ", end='\r') 
                
            elif key == readchar.key.DOWN:
                self.xoffset -= 0.01
                print(f"Current offset: {self.xoffset}    ", end='\r')
                
            elif key.lower() == 's':
                print(f"\n[Saved] Number stored as: {self.xoffset}")
                print(f"Current number: {self.xoffset}    ", end='\r')
                break

        print("Calibrate the X axis")
        print(f"Starting at {self.yoffset}")
        while True:
            key = readchar.readkey()

            if key == readchar.key.UP:
                self.yoffset += 0.01
                print(f"Current offset: {self.yoffset}    ", end='\r') 
                
            elif key == readchar.key.DOWN:
                self.yoffset -= 0.01
                print(f"Current offset: {self.yoffset}    ", end='\r')
                
            elif key.lower() == 's':
                print(f"\n[Saved] Number stored as: {self.yoffset}")
                print(f"Current number: {self.yoffset}    ", end='\r')
                break   

    def dcinit(self):
        self.device.write(f":OUTPut1:LOAD INFinity;:OUTPut2:LOAD INFinity")
        self.device.write(f":SOURce1:APPLy:DC 1,1,{self.xoffset:.3f}")
        self.device.write(f":SOURce2:APPLy:DC 1,1,{self.yoffset:.3f}")
        self.device.write(":OUTP1 ON;:OUTP2 ON")

    def dcupdate(self, ch:int, val:float):
        if ch == 1:
            self.xpos = val
            val = self.xpos + self.xoffset
        if ch == 2:
            self.ypos = val 
            val = self.ypos + self.yoffset
        self.device.write(f"SOURce{ch}:VOLTage:OFFSet {val:.3f}")

    def move(self, ch:int, endval:float, t:float=1.0, resolution:int=60):
        if ch == 1:
            if endval > self.xpos:
                step = 1/resolution
            if endval < self.xpos:
                step = -1/resolution
            else: return
            for i in np.arange(self.xpos, endval, step):
                self.device.write(f"SOURce1:VOLTage:OFFSet {(self.xoffset + i):.3f}")
                time.sleep(t/resolution)
            self.xpos = endval

        if ch == 2:
            if endval > self.ypos:
                step = 1/resolution
            if endval < self.ypos:
                step = -1/resolution
            else: return
            for i in np.arange(self.ypos, endval, step):
                self.device.write(f"SOURce2:VOLTage:OFFSet {(self.yoffset + i):.3f}")
                time.sleep(t/resolution)
            self.ypos = endval
    
    def sininit(self, freq=0.0, amp=0.0, phase=0.0):
        if freq == 0 and freq != self.freq:
            freq = self.freq
        if amp == 0 and amp != self.amp:
            amp = self.amp
        if phase == 0 and phase != self.phase:
            phase = self.phase

        x = self.xoffset + self.xpos
        y = self.yoffset + self.ypos
        self.device.write(f":SOUR1:APPL:SIN {freq},{amp},{x},{phase}")
        self.device.write(f":SOUR2:APPL:SIN {freq},{amp},{y},{phase}")

    def sinupdate(self, ch:int, freq=0, amp=0, phase=0):
        if freq == 0:
            freq = self.freq
        if amp == 0:
            amp = self.amp
        if phase == 0:
            phase = self.phase

        self.device.write(f":SOURce{ch}:FREQ {freq}")
        self.device.write(f":SOURce{ch}:PHAS {phase}")
        self.device.write(f":SOURce{ch}:VOLT {amp}")

        self.freq = freq
        self.amp = amp
        self.phase = phase
