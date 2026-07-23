import pyvisa
import time
import keyboard

rm  = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 20000
dev.query("*IDN?")
dev.write(f"SOURce1:APPLy:DC 1,1,0")
dev.write(f"OUTP1 ON")

X, Y = 0,0

while True:
    # Check individual keys continuously
    if keyboard.is_pressed('w') and Y <= 2 and Y >= -2:
        Y += 1/100
    if keyboard.is_pressed('s') and Y <= 2 and Y >= -2:
        Y += -1/100
    if keyboard.is_pressed('a') and X <= 2 and X >= -2:
        X += 1/100
    if keyboard.is_pressed('d') and X <= 2 and X >= -2:
        X += -1/100
    # Exit condition
    if keyboard.is_pressed('esc'):
        print("Exiting game.")
        break
    dev.write(f"SOURce1:APPly:DC 1,1,{Y}")
    dev.write(f"SOURce1:APPly:DC 1,1,{X}")
    time.sleep(1/100)  # Small delay to keep CPU usage down

