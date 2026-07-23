import time
import numpy as np
import pyvisa

def create_custom_waveform(num_points=1000):
    """
    Generates a normalized array of floating point numbers 
    between -1.0 and 1.0.
    """
    t = np.linspace(0, 2 * np.pi, num_points)
    # Example custom wave: fundamental sine + 3rd harmonic overshoot
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
    
    # Open connection to the first USB device (adjust index if multiple instruments are plugged in)
    dg1022z = rm.open_resource(resources[0])
    dg1022z.timeout = 5000  # 5 second timeout
    
    # Optional: Verify identity
    idn = dg1022z.query('*IDN?')
    print(f"Connected to: {idn.strip()}")

    # 2. Generate sample waveform data
    points = create_custom_waveform(num_points=1000)
    
    # Convert numbers to a comma-separated string format
    # format: ",val1,val2,val3..."
    data_str = "," + ",".join([f"{val:.4f}" for val in points])

    print("Uploading waveform data...")
    
    # 3. Send SCPI Commands to DG1022Z
    # Select Channel 1
    dg1022z.write(":SOURce1:FUNCtion ARBitrary")
    
    # Send waveform vector to volatile memory
    dg1022z.write(f":SOURce1:DATA VOLATILE{data_str}")
    
    # Set the channel to use the VOLATILE memory waveform
    dg1022z.write(":SOURce1:FUNCtion:ARBitrary VOLATILE")
    
    # 4. Configure Output Properties (Frequency, Amplitude, Offset)
    dg1022z.write(":SOURce1:FREQuency 1000")       # 1 kHz
    dg1022z.write(":SOURce1:VOLTage 2.0")          # 2.0 Vpp
    dg1022z.write(":SOURce1:VOLTage:OFFSet 0.0")   # 0 V Offset
    
    # 5. Enable the channel output
    dg1022z.write(":OUTPUT1 ON")
    print("Waveform successfully sent and Channel 1 enabled!")

    # Close resource connection
    dg1022z.close()

if __name__ == "__main__":
    upload_and_run()