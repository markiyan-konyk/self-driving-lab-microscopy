from gpiozero import OutputDevice
from time import sleep

# GPIO23 = physical pin 16
relay = OutputDevice(23, active_high=True, initial_value=False)

print("Laser OFF")

sleep(2)

print("Laser ON")
relay.on()

sleep(5)

print("Laser OFF")
relay.off()