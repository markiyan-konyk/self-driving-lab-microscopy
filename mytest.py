import pyvisa
import pygame as pg
import time

rm  = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 20000

dev.query("*IDN?")
dev.write(f"SOURce1:APPLy:DC 1,1,0")
dev.write(f"OUTP1 ON")

for y in range(30000):
    i = y * (4/30000)
    dev.write(f"SOURce1:APPLy:DC 1,1,{i}")
    time.sleep(1/10000)

