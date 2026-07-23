import time
import numpy as np
import pyvisa

def create_custom_waveform(num_points=1000):
    """
    Generates a normalized array of floating point numbers 
    between -1.0 and 1.0.
    """
    t = np.linspace(0, 2 * np.pi, num_points)
    # Custom wave: fundamental sine + 3rd harmonic overshoot
    wave = np.sin(t) + 0.3 * np.sin(3 * t)
    
    # Normalize wave strictly between -1.0 and +1.0
    wave_max = np.max(np.abs(wave))
    normalized_wave = wave / wave_max
    
    return normalized_wave

def upload_and_run():
    # 1. Initialize VISA Resource Manager
    rm = pyvisa.ResourceManager()
    
    # Find connected USB instruments
    resources = rm.list_resources('USB?*INSTR')
    if not resources:
        raise RuntimeError("No USB VISA instruments found. Check physical connection.")
    
    print(f"Found instruments: {resources}")
    
    # Open connection
    dg1022 = rm.open_resource(resources[0])
    dg1022.timeout = 5000  # 5 second timeout
    
    # Verify identity
    idn = dg1022.query('*IDN?')
    print(f"Connected to: {idn.strip()}")

    # 2. Generate sample waveform data
    points = create_custom_waveform(num_points=1000)
    
    # Format floating point array into comma-separated string
    data_str = ",".join([f"{val:.4f}" for val in points])

    print("Uploading waveform data...")
    
    # 3. Send SCPI Commands to DG1022 (Legacy SCPI Syntax)
    #
    # Upload data directly to VOLATILE memory.
    # DG1022 format: DATA VOLATILE,val1,val2,...
    dg1022.write(f"DATA VOLATILE,{data_str}")
    
    # Select VOLATILE memory as the current custom waveform output
    # Note: VOLATILE cannot be abbreviated here
    dg1022.write("FUNC:USER VOLATILE")
    
    # Switch output function to USER (Arbitrary)
    dg1022.write("FUNC USER")
    
    # 4. Configure Output Properties (Frequency, Amplitude, Offset)
    # Note: No :SOURce1: prefix for Channel 1 on DG1022
    dg1022.write("FREQ 2")          # 1 kHz
    dg1022.write("VOLT 2.0")           # 2.0 Vpp
    dg1022.write("VOLT:OFFS 0.0")      # 0 V Offset
    
    # 5. Enable Output
    # DG1022 uses OUTP ON (or OUTP:CH2 ON for Channel 2)
    dg1022.write("OUTP ON")
    print("Waveform successfully sent and Channel 1 enabled!")

    # Close connection
    dg1022.close()

if __name__ == "__main__":
    upload_and_run()