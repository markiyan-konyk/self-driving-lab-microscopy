import pyvisa

rm = pyvisa.ResourceManager()
print(rm.list_resources())

# Something like this should appear: USB0::0x1AB1::0x0642::DG1ZA...::INSTR,
# Use that when calling the Galvo class
