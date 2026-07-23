import pyvisa
import time
import pygame

pygame.init()
screen = pygame.display.set_mode((400, 400))
pygame.display.set_caption("Galvo Controller")

x, y = 200, 200
clock = pygame.time.Clock()
running = True

rm  = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 20000
dev.query("*IDN?")

# 1. EXPLICIT PURE DC INITIALIZATION
# Set channels to High Impedance (best for galvo driver inputs)
dev.write(":OUTPut1:LOAD INFinity;:OUTPut2:LOAD INFinity")
# Use explicit FUNCtion DC instead of the APPLy macro to avoid attenuator staging
dev.write(":SOURce1:FUNCtion DC;:SOURce2:FUNCtion DC")
dev.write(":OUTP1 ON;:OUTP2 ON")

X, Y = 0.0, 0.0

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

    keys = pygame.key.get_pressed()

    if keys[pygame.K_w] and Y < 2:
        y -= 1
        Y -= 0.01
        dev.write(f":SOURce2:VOLTage:OFFSet {Y:.3f}")
        time.sleep(1/60)
    if keys[pygame.K_s] and Y < -2:
        y += 1
        Y += 0.01
        dev.write(f":SOURce2:VOLTage:OFFSet {Y:.3f}")
        time.sleep(1/60)
    if keys[pygame.K_a] and X > -2:  
        x -= 1
        X -= 0.01
        dev.write(f":SOURce1:VOLTage:OFFSet {X:.3f}")
        time.sleep(1/60)
    if keys[pygame.K_d] and X < 2:
        x += 1
        X += 0.01
        dev.write(f":SOURce1:VOLTage:OFFSet {X:.3f}")
        time.sleep(1/60)
        
    x = max(0, min(400, x))
    y = max(0, min(400, y))
    
    screen.fill((0, 0, 0))
    pygame.draw.circle(screen, (255, 0, 0), (x, y), 5)
    pygame.display.flip()
    clock.tick(60)

dev.write(":OUTP1 OFF;:OUTP2 OFF")
dev.close()
pygame.quit()
