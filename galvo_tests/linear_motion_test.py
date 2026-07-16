import numpy as np
import time

def move_s_curve(galvo_controller, x1, y1, x2, y2, duration_sec, 
                 sample_rate_hz=2000, s_factor=4.0):
    """
    Move the laser from point A (x1, y1) to point B (x2, y2) using a tanh S-curve.
    This minimizes mechanical vibration and ensures the smoothest possible motion.

    :param galvo_controller: Instance of GalvoController class.
    :param x1, y1: Starting coordinates (degrees).
    :param x2, y2: Target coordinates (degrees).
    :param duration_sec: Total travel time (seconds).
    :param sample_rate_hz: Output update rate (Hz). Default 2000 Hz.
    :param s_factor: Steepness control. Higher = more abrupt at ends (closer to step),
                     Lower = more gradual (closer to linear ramp). Default 4.0 is optimal.
    """
    if duration_sec <= 0:
        # Instant jump to target
        galvo_controller.set_static_angle(1, x2)
        galvo_controller.set_static_angle(2, y2)
        return

    # 1. Calculate total points (capped at DG1000Z's 16384-point memory limit)
    total_points = int(duration_sec * sample_rate_hz)
    if total_points > 16384:
        sample_rate_hz = 16384 / duration_sec
        total_points = 16384
        print(f"Warning: Total points exceeded limit. Adjusted sample rate to {sample_rate_hz:.1f} Hz")
    elif total_points < 10:
        # Ensure at least 10 points for a meaningful curve
        total_points = 10
        sample_rate_hz = total_points / duration_sec

    # 2. Generate normalized time vector from -s_factor to +s_factor
    #    The tanh curve is most active (S-shape) between -4 and +4.
    t = np.linspace(-s_factor, s_factor, total_points)
    
    # 3. Apply tanh and normalize to [0, 1] range
    #    tanh(-inf) = -1, tanh(+inf) = +1, so (tanh(t) + 1) / 2 => 0 ~ 1
    normalized_position = (np.tanh(t) + 1.0) / 2.0

    # 4. Calculate start/end voltages (assuming volt_per_deg scaling is set in the controller)
    vx1 = x1 * galvo_controller.volt_per_deg
    vx2 = x2 * galvo_controller.volt_per_deg
    vy1 = y1 * galvo_controller.volt_per_deg
    vy2 = y2 * galvo_controller.volt_per_deg

    # 5. Interpolate voltages using the S-curve weights
    x_voltage_array = (vx1 + (vx2 - vx1) * normalized_position).tolist()
    y_voltage_array = (vy1 + (vy2 - vy1) * normalized_position).tolist()

    # 6. Upload and play the waveforms on both channels simultaneously
    print(f"Moving S-curve from ({x1:.2f}, {y1:.2f}) to ({x2:.2f}, {y2:.2f}) over {duration_sec}s...")
    galvo_controller.set_arbitrary_pattern(1, x_voltage_array, sample_rate_hz)
    galvo_controller.set_arbitrary_pattern(2, y_voltage_array, sample_rate_hz)

    # 7. Wait for the movement to complete
    time.sleep(duration_sec + 0.05)

    # 8. Important: Lock the final position (stop looping and hold at target)
    galvo_controller.set_static_angle(1, x2)
    galvo_controller.set_static_angle(2, y2)
    print("S-curve movement completed and held at target.")

move_s_curve(galvo, x1=0, y1=0, x2=5, y2=3, duration_sec=2.0, s_factor=4.0)
