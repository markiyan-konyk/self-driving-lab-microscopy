#!/usr/bin/env python3
"""
Galvo Controller with Stepwise Motion
EXACTLY like mytest.py, but moves toward a clicked target.
"""

import sys
import time
import pygame
import pyvisa

# Constants
WINDOW_SIZE = 400
CENTER = WINDOW_SIZE // 2
VOLT_RANGE = 2.0
PIXELS_PER_VOLT = CENTER / VOLT_RANGE
STEP_SIZE = 0.01

# Pygame init
pygame.init()
screen = pygame.display.set_mode((WINDOW_SIZE, WINDOW_SIZE))
pygame.display.set_caption("Stepwise Galvo (mytest.py style)")
font = pygame.font.Font(None, 24)
clock = pygame.time.Clock()
running = True

# Connect to DG1000Z
rm = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 30000
dev.write("*RST")
time.sleep(1.0)
idn = dev.query("*IDN?")
print(f"Connected to: {idn}")

# DC INITIALIZATION (same as mytest.py)
dev.write(":OUTPut1:LOAD INFinity;:OUTPut2:LOAD INFinity")
dev.write(":SOURce1:FUNCtion DC;:SOURce2:FUNCtion DC")
dev.write(":OUTP1 ON;:OUTP2 ON")

# Current position (volts)
current_X = 0.0
current_Y = 0.0

# Target position (volts)
target_X = 0.0
target_Y = 0.0

# Motion flag (True when target is set)
moving = False

# ============================================================
# Helper functions
# ============================================================
def pixel_to_voltage(px, py):
    vx = (px - CENTER) / PIXELS_PER_VOLT
    vy = (py - CENTER) / PIXELS_PER_VOLT
    return vx, vy

def voltage_to_pixel(vx, vy):
    px = int(CENTER + vx * PIXELS_PER_VOLT)
    py = int(CENTER + vy * PIXELS_PER_VOLT)
    px = max(0, min(WINDOW_SIZE-1, px))
    py = max(0, min(WINDOW_SIZE-1, py))
    return px, py

def set_target(vx, vy):
    global target_X, target_Y, moving
    target_X = max(-VOLT_RANGE, min(VOLT_RANGE, vx))
    target_Y = max(-VOLT_RANGE, min(VOLT_RANGE, vy))
    moving = True
    print(f"Target set to X={target_X:.3f}V, Y={target_Y:.3f}V")

# ============================================================
# Main loop (structure is IDENTICAL to mytest.py)
# ============================================================
while running:
    # --- Event handling ---
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                running = False
            elif event.key == pygame.K_r:
                set_target(0.0, 0.0)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:  # Left click
                mx, my = pygame.mouse.get_pos()
                vx, vy = pixel_to_voltage(mx, my)
                print(f"Click at ({mx},{my}) -> target ({vx:.3f}V, {vy:.3f}V)")
                set_target(vx, vy)

    # --- Motion update (EXACTLY like mytest.py's WASD logic) ---
    # Each axis is checked independently in the same loop iteration.
    # Just like mytest.py checks if keys[K_w] and keys[K_a] in the same frame.
    if moving:
        moved = False

        # X axis (like mytest.py's A/D keys)
        if target_X > current_X and current_X < VOLT_RANGE:
            current_X += STEP_SIZE
            if current_X > target_X:
                current_X = target_X
            dev.write(f":SOURce1:VOLTage:OFFSet {current_X:.3f}")
            time.sleep(1/60)      # ← Exactly like mytest.py!
            moved = True
        elif target_X < current_X and current_X > -VOLT_RANGE:
            current_X -= STEP_SIZE
            if current_X < target_X:
                current_X = target_X
            dev.write(f":SOURce1:VOLTage:OFFSet {current_X:.3f}")
            time.sleep(1/60)      # ← Exactly like mytest.py!
            moved = True

        # Y axis (like mytest.py's W/S keys)
        if target_Y > current_Y and current_Y < VOLT_RANGE:
            current_Y += STEP_SIZE
            if current_Y > target_Y:
                current_Y = target_Y
            dev.write(f":SOURce2:VOLTage:OFFSet {current_Y:.3f}")
            time.sleep(1/60)      # ← Exactly like mytest.py!
            moved = True
        elif target_Y < current_Y and current_Y > -VOLT_RANGE:
            current_Y -= STEP_SIZE
            if current_Y < target_Y:
                current_Y = target_Y
            dev.write(f":SOURce2:VOLTage:OFFSet {current_Y:.3f}")
            time.sleep(1/60)      # ← Exactly like mytest.py!
            moved = True

        # Stop moving if both axes are exactly at target
        if abs(target_X - current_X) < 1e-6 and abs(target_Y - current_Y) < 1e-6:
            moving = False
            print("Reached target!")

    # --- Drawing (identical UI) ---
    screen.fill((0, 0, 0))

    # Grid lines (every 0.5V)
    grid_color = (40, 40, 40)
    for v in range(-2, 3, 1):
        if v == 0:
            continue
        px = CENTER + int(v * PIXELS_PER_VOLT)
        if 0 <= px < WINDOW_SIZE:
            pygame.draw.line(screen, grid_color, (px, 0), (px, WINDOW_SIZE), 1)
            pygame.draw.line(screen, grid_color, (0, px), (WINDOW_SIZE, px), 1)
    # Axes
    pygame.draw.line(screen, (80, 80, 80), (0, CENTER), (WINDOW_SIZE, CENTER), 1)
    pygame.draw.line(screen, (80, 80, 80), (CENTER, 0), (CENTER, WINDOW_SIZE), 1)
    pygame.draw.circle(screen, (100, 100, 100), (CENTER, CENTER), 3, 1)

    # Target point (green hollow)
    if moving or (target_X != 0 or target_Y != 0):
        tx, ty = voltage_to_pixel(target_X, target_Y)
        pygame.draw.circle(screen, (0, 255, 0), (tx, ty), 6, 2)

    # Current position (red dot)
    cx, cy = voltage_to_pixel(current_X, current_Y)
    pygame.draw.circle(screen, (255, 0, 0), (cx, cy), 5)

    # UI Text
    coord_text = font.render(f"X: {current_X:+.3f}V  Y: {current_Y:+.3f}V", True, (200, 200, 200))
    screen.blit(coord_text, (10, WINDOW_SIZE - 30))
    info_text = font.render("Click: target  |  R: Origin  |  ESC: Quit", True, (150, 150, 150))
    screen.blit(info_text, (10, WINDOW_SIZE - 60))
    target_text = font.render(f"Target: ({target_X:+.2f}, {target_Y:+.2f})", True, (150, 200, 150))
    screen.blit(target_text, (10, 10))

    pygame.display.flip()
    clock.tick(60)

# --- Cleanup ---
dev.write(":OUTP1 OFF;:OUTP2 OFF")
dev.close()
pygame.quit()
sys.exit()
