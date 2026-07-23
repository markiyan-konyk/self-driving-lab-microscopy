#!/usr/bin/env python3
"""
Galvo Controller - Sequential Motion with Debug Prints
Click anytime to set new target.
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
STEP = 0.01
SLEEP = 1 / 60

# Pygame init
pygame.init()
screen = pygame.display.set_mode((WINDOW_SIZE, WINDOW_SIZE))
pygame.display.set_caption("Sequential Galvo (X → Y)")
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

# Current position
curX = 0.0
curY = 0.0

# Target position
targetX = 0.0
targetY = 0.0

# Motion state: 0=idle, 1=moving X, 2=moving Y
state = 0

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
    global targetX, targetY, state
    targetX = max(-VOLT_RANGE, min(VOLT_RANGE, vx))
    targetY = max(-VOLT_RANGE, min(VOLT_RANGE, vy))
    state = 1
    print(f"🔵 SET TARGET: ({targetX:.3f}V, {targetY:.3f}V) → state=1")

def update_sequential():
    global curX, curY, state
    # --- X axis ---
    if state == 1:
        if abs(curX - targetX) < STEP / 2:
            curX = targetX
            dev.write(f":SOURce1:VOLTage:OFFSet {curX:.3f}")
            time.sleep(SLEEP)
            print(f"  ✅ X reached: {curX:.3f} → moving to Y")
            state = 2
        elif targetX > curX:
            curX += STEP
            if curX > targetX: curX = targetX
            dev.write(f":SOURce1:VOLTage:OFFSet {curX:.3f}")
            time.sleep(SLEEP)
        elif targetX < curX:
            curX -= STEP
            if curX < targetX: curX = targetX
            dev.write(f":SOURce1:VOLTage:OFFSet {curX:.3f}")
            time.sleep(SLEEP)

    # --- Y axis ---
    elif state == 2:
        if abs(curY - targetY) < STEP / 2:
            curY = targetY
            dev.write(f":SOURce2:VOLTage:OFFSet {curY:.3f}")
            time.sleep(SLEEP)
            print(f"  ✅ Y reached: {curY:.3f} → motion complete, back to IDLE")
            state = 0
        elif targetY > curY:
            curY += STEP
            if curY > targetY: curY = targetY
            dev.write(f":SOURce2:VOLTage:OFFSet {curY:.3f}")
            time.sleep(SLEEP)
        elif targetY < curY:
            curY -= STEP
            if curY < targetY: curY = targetY
            dev.write(f":SOURce2:VOLTage:OFFSet {curY:.3f}")
            time.sleep(SLEEP)

# ============================================================
# Main Loop
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
                print("🔄 Origin requested")
                set_target(0.0, 0.0)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:
                mx, my = pygame.mouse.get_pos()
                vx, vy = pixel_to_voltage(mx, my)
                print(f"🖱️ Click at ({mx},{my}) → ({vx:.3f}V, {vy:.3f}V)")
                set_target(vx, vy)

    # --- Update motion ---
    if state == 1 or state == 2:
        update_sequential()

    # --- Drawing ---
    screen.fill((0, 0, 0))

    # Grid
    for v in range(-2, 3, 1):
        if v == 0: continue
        px = CENTER + int(v * PIXELS_PER_VOLT)
        if 0 <= px < WINDOW_SIZE:
            pygame.draw.line(screen, (40,40,40), (px,0), (px,WINDOW_SIZE), 1)
            pygame.draw.line(screen, (40,40,40), (0,px), (WINDOW_SIZE,px), 1)
    pygame.draw.line(screen, (80,80,80), (0,CENTER), (WINDOW_SIZE,CENTER), 1)
    pygame.draw.line(screen, (80,80,80), (CENTER,0), (CENTER,WINDOW_SIZE), 1)
    pygame.draw.circle(screen, (100,100,100), (CENTER,CENTER), 3, 1)

    # Target (green hollow)
    if state != 0 or (targetX != 0 or targetY != 0):
        tx, ty = voltage_to_pixel(targetX, targetY)
        pygame.draw.circle(screen, (0, 255, 0), (tx, ty), 6, 2)

    # Current (red dot)
    cx, cy = voltage_to_pixel(curX, curY)
    pygame.draw.circle(screen, (255, 0, 0), (cx, cy), 5)

    # Info text
    state_text = ["IDLE", "MOVING X", "MOVING Y"][state]
    screen.blit(font.render(f"X: {curX:+.3f}V  Y: {curY:+.3f}V", True, (200,200,200)), (10, WINDOW_SIZE-30))
    screen.blit(font.render(f"State: {state_text}", True, (200,200,200)), (10, WINDOW_SIZE-60))
    screen.blit(font.render(f"Target: ({targetX:+.2f}, {targetY:+.2f})", True, (150,200,150)), (10, 10))
    screen.blit(font.render("Click: target  |  R: Origin  |  ESC: Quit", True, (150,150,150)), (10, 40))

    pygame.display.flip()
    clock.tick(60)

# Cleanup
dev.write(":OUTP1 OFF;:OUTP2 OFF")
dev.close()
pygame.quit()
sys.exit()
