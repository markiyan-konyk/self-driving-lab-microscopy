#!/usr/bin/env python3
"""
Galvo Controller with Smooth Point-to-Point Motion

Click on the window to set a target; the laser moves smoothly to that point.
Uses the same DC initialization as mytest.py to avoid glitches.
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

# Smooth motion parameters
MOVE_STEPS = 100          # Number of interpolation steps
MOVE_DURATION = 2.0       # Total duration in seconds
STEP_INTERVAL = MOVE_DURATION / MOVE_STEPS

# Initialize Pygame
pygame.init()
screen = pygame.display.set_mode((WINDOW_SIZE, WINDOW_SIZE))
pygame.display.set_caption("Smooth Galvo Controller (Click to move)")
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

# Current voltage position
current_X = 0.0
current_Y = 0.0

# Motion state
moving = False
target_X = 0.0
target_Y = 0.0
path_X = []
path_Y = []
step_index = 0

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
# Smooth move: generate path and start motion
# ============================================================
def start_smooth_move(target_vx, target_vy):
    global moving, current_X, current_Y, target_X, target_Y, path_X, path_Y, step_index
    target_X = target_vx
    target_Y = target_vy
    # Generate linear interpolation from current to target
    path_X = [current_X + (target_X - current_X) * i / MOVE_STEPS for i in range(MOVE_STEPS + 1)]
    path_Y = [current_Y + (target_Y - current_Y) * i / MOVE_STEPS for i in range(MOVE_STEPS + 1)]
    step_index = 0
    moving = True

# ============================================================
# Update: send next voltage step if moving
# ============================================================
def update_motion():
    global current_X, current_Y, moving, step_index
    if moving and step_index < len(path_X):
        vx = path_X[step_index]
        vy = path_Y[step_index]
        # Send voltage updates
        dev.write(f":SOURce1:VOLTage:OFFSet {vx:.3f}")
        dev.write(f":SOURce2:VOLTage:OFFSet {vy:.3f}")
        current_X = vx
        current_Y = vy
        step_index += 1
        if step_index >= len(path_X):
            moving = False
            print(f"Reached target: X={current_X:.3f}V, Y={current_Y:.3f}V")
        return True
    return False

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
                if not moving:
                    start_smooth_move(0.0, 0.0)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:  # Left click
                if not moving:
                    mx, my = pygame.mouse.get_pos()
                    vx, vy = pixel_to_voltage(mx, my)
                    # Clamp to ±2V
                    vx = max(-VOLT_RANGE, min(VOLT_RANGE, vx))
                    vy = max(-VOLT_RANGE, min(VOLT_RANGE, vy))
                    print(f"Click at ({mx},{my}) -> target ({vx:.3f}V, {vy:.3f}V)")
                    start_smooth_move(vx, vy)

    # Update motion (non-blocking)
    update_motion()

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

    # Draw target point (if any)
    if not moving and (target_X != 0 or target_Y != 0):
        tx, ty = voltage_to_pixel(target_X, target_Y)
        pygame.draw.circle(screen, (0, 255, 0), (tx, ty), 6, 2)   # Green hollow

    # Draw current position (red dot)
    cx, cy = voltage_to_pixel(current_X, current_Y)
    pygame.draw.circle(screen, (255, 0, 0), (cx, cy), 5)

    # Draw path progress (if moving)
    if moving:
        # Draw remaining path as a line from current to target
        tx, ty = voltage_to_pixel(target_X, target_Y)
        pygame.draw.line(screen, (255, 255, 0), (cx, cy), (tx, ty), 1)
        # Progress text
        progress = int(step_index / MOVE_STEPS * 100)
        text = font.render(f"Moving: {progress}%", True, (200, 200, 200))
        screen.blit(text, (10, 10))

    # Show coordinates
    coord_text = font.render(f"X: {current_X:+.3f}V  Y: {current_Y:+.3f}V", True, (200, 200, 200))
    screen.blit(coord_text, (10, WINDOW_SIZE - 30))
    info_text = font.render("Click to move  |  R: Origin  |  ESC: Quit", True, (150, 150, 150))
    screen.blit(info_text, (10, WINDOW_SIZE - 60))

    pygame.display.flip()
    clock.tick(60)

# ============================================================
# Cleanup
# ============================================================
dev.write(":OUTP1 OFF;:OUTP2 OFF")
dev.close()
pygame.quit()
sys.exit()
