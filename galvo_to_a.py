#!/usr/bin/env python3
"""
Galvo Controller with Stepwise Motion (exactly like mytest.py)

Click to set a target; the laser moves step by step (0.01V per frame)
until it reaches the target. Speed matches mytest.py (1/60 sec per step).
"""

import sys
import time
import math
import pygame
import pyvisa

# Constants
WINDOW_SIZE = 400
CENTER = WINDOW_SIZE // 2
VOLT_RANGE = 2.0          # ±2V
PIXELS_PER_VOLT = CENTER / VOLT_RANGE  # 100 pixels per volt

# Initialize Pygame
pygame.init()
screen = pygame.display.set_mode((WINDOW_SIZE, WINDOW_SIZE))
pygame.display.set_caption("Stepwise Galvo Controller (Click to move)")
font = pygame.font.Font(None, 24)
clock = pygame.time.Clock()
running = True

# Connect to DG1000Z
rm = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 20000
dev.query("*IDN?")

# ============================================================
# DC INITIALIZATION (same as mytest.py)
# ============================================================
dev.write(":OUTPut1:LOAD INFinity;:OUTPut2:LOAD INFinity")
dev.write(":SOURce1:FUNCtion DC;:SOURce2:FUNCtion DC")
dev.write(":OUTP1 ON;:OUTP2 ON")

# Current position (volts)
current_X = 0.0
current_Y = 0.0

# Target position (volts)
target_X = 0.0
target_Y = 0.0

# Motion flag
moving = False

# ============================================================
# Helper: Convert pixel to voltage
# ============================================================
def pixel_to_voltage(px, py):
    vx = (px - CENTER) / PIXELS_PER_VOLT
    vy = (py - CENTER) / PIXELS_PER_VOLT
    return vx, vy

# ============================================================
# Helper: Convert voltage to pixel (for drawing)
# ============================================================
def voltage_to_pixel(vx, vy):
    px = int(CENTER + vx * PIXELS_PER_VOLT)
    py = int(CENTER + vy * PIXELS_PER_VOLT)
    # Clamp to window bounds
    px = max(0, min(WINDOW_SIZE-1, px))
    py = max(0, min(WINDOW_SIZE-1, py))
    return px, py

# ============================================================
# Stepwise motion update (exactly like mytest.py)
# Called once per frame.
# ============================================================
def update_stepwise_motion():
    global current_X, current_Y, moving, x, y

    # If we are not at the target, move one step (0.01V) per axis
    # toward the target, exactly as mytest.py does with WASD keys.
    step = 0.01
    moved = False

    # X axis
    if abs(target_X - current_X) > step / 2:  # avoid overshoot
        if target_X > current_X:
            current_X += step
            moved = True
        elif target_X < current_X:
            current_X -= step
            moved = True
    else:
        # Snap to exact target if very close
        if current_X != target_X:
            current_X = target_X
            moved = True

    # Y axis
    if abs(target_Y - current_Y) > step / 2:
        if target_Y > current_Y:
            current_Y += step
            moved = True
        elif target_Y < current_Y:
            current_Y -= step
            moved = True
    else:
        if current_Y != target_Y:
            current_Y = target_Y
            moved = True

    # If we moved, send the voltage commands (same as mytest.py)
    if moved:
        dev.write(f":SOURce1:VOLTage:OFFSet {current_X:.3f}")
        dev.write(f":SOURce2:VOLTage:OFFSet {current_Y:.3f}")
        # Small delay to match mytest.py's speed (1/60 sec per step)
        time.sleep(1/60)

    # Update visual dot coordinates (like mytest.py)
    # Convert current voltage to pixel position
    px, py = voltage_to_pixel(current_X, current_Y)
    x = px
    y = py

    # If we reached the target exactly, stop moving
    if abs(target_X - current_X) < 1e-6 and abs(target_Y - current_Y) < 1e-6:
        moving = False

    return moved

# ============================================================
# Set target and start moving (stepwise)
# ============================================================
def set_target(vx, vy):
    global target_X, target_Y, moving
    target_X = vx
    target_Y = vy
    moving = True
    print(f"Target set to X={vx:.3f}V, Y={vy:.3f}V")

# ============================================================
# Main loop
# ============================================================
while running:
    # Event handling
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                running = False
            elif event.key == pygame.K_r:
                # Return to origin
                set_target(0.0, 0.0)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:  # Left click
                mx, my = pygame.mouse.get_pos()
                vx, vy = pixel_to_voltage(mx, my)
                # Clamp to ±2V
                vx = max(-VOLT_RANGE, min(VOLT_RANGE, vx))
                vy = max(-VOLT_RANGE, min(VOLT_RANGE, vy))
                print(f"Click at ({mx},{my}) -> target ({vx:.3f}V, {vy:.3f}V)")
                set_target(vx, vy)

    # Update motion (stepwise) if moving
    if moving:
        update_stepwise_motion()

    # Draw everything
    screen.fill((0, 0, 0))

    # Draw grid lines (every 0.5V)
    grid_color = (40, 40, 40)
    for v in range(-2, 3, 1):
        if v == 0:
            continue
        px = CENTER + int(v * PIXELS_PER_VOLT)
        if 0 <= px < WINDOW_SIZE:
            pygame.draw.line(screen, grid_color, (px, 0), (px, WINDOW_SIZE), 1)
            pygame.draw.line(screen, grid_color, (0, px), (WINDOW_SIZE, px), 1)
    # Draw axes
    pygame.draw.line(screen, (80, 80, 80), (0, CENTER), (WINDOW_SIZE, CENTER), 1)
    pygame.draw.line(screen, (80, 80, 80), (CENTER, 0), (CENTER, WINDOW_SIZE), 1)
    # Origin cross
    pygame.draw.circle(screen, (100, 100, 100), (CENTER, CENTER), 3, 1)

    # Draw target point (green hollow circle)
    if moving or (target_X != 0 or target_Y != 0):
        tx, ty = voltage_to_pixel(target_X, target_Y)
        pygame.draw.circle(screen, (0, 255, 0), (tx, ty), 6, 2)

    # Draw current position (red dot)
    cx, cy = voltage_to_pixel(current_X, current_Y)
    pygame.draw.circle(screen, (255, 0, 0), (cx, cy), 5)

    # Show coordinates
    coord_text = font.render(f"X: {current_X:+.3f}V  Y: {current_Y:+.3f}V", True, (200, 200, 200))
    screen.blit(coord_text, (10, WINDOW_SIZE - 30))
    info_text = font.render("Click to set target  |  R: Origin  |  ESC: Quit", True, (150, 150, 150))
    screen.blit(info_text, (10, WINDOW_SIZE - 60))

    # Show target coordinates
    target_text = font.render(f"Target: ({target_X:+.2f}, {target_Y:+.2f})", True, (150, 200, 150))
    screen.blit(target_text, (10, 10))

    pygame.display.flip()
    clock.tick(60)

# ============================================================
# Cleanup
# ============================================================
dev.write(":OUTP1 OFF;:OUTP2 OFF")
dev.close()
pygame.quit()
sys.exit()
