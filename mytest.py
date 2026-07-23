import pyvisa
import pygame as pg
import time

rm  = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 20000

dev.query("*IDN?")
dev.write(f"SOURce1:APPLy:DC 1,1,0")

for x in range(1,10001,5):
    for y in range(x):
        i = y * (3/x)
        dev.write(f"SOURce1:APPLy:DC 1,1,{i}")
        time.sleep(1/x)
    time.sleep(0.25)
