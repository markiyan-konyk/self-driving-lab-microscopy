# Live state of the sample-temperature controller (Wavelength TC LAB), polled
# by temperature_node. This is the READ side: everything a client needs to plot
# a curve or decide "am I at temperature yet" without issuing any command.
# The WRITE side is the generic temperature/call service (InstrumentCall) --
# the node exposes the whole driver class, not a fixed set of setters.
std_msgs/Header header
bool connected
string idn                # *IDN? of the connected instrument
float32 temperature       # control sensor, in `units`
float32 setpoint          # in `units`
float32 aux_temperature   # aux sensor -- ALWAYS Celsius per the instrument
float32 tec_current       # A (signed: + heats, - cools)
float32 tec_voltage       # V
uint8 units               # 0=Celsius 1=Kelvin 2=Fahrenheit 3=raw sensor
bool output_enabled       # TEC current enabled (TEC:OUTput)
bool in_tolerance         # instrument's own tolerance window is satisfied
bool at_current_limit     # output is clamped by the current limit
bool sensor_fault         # sensor open or shorted -- readings are meaningless
int32 condition           # raw TEC:COND? register, for anything not decoded above
string last_method        # last driver method a client called through the node
string last_error         # last error (empty if none)
