import pyvisa
import time
import pygame

pygame.init()
screen = pygame.display.set_mode((400, 400))
pygame.display.set_caption("Galvo Controller")

# Initial galvo laser coordinates
x, y = 200, 200
clock = pygame.time.Clock()
running = True

rm  = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 20000
dev.query("*IDN?")

# 1. INITIALIZE ONCE
# Configure the channels to DC mode before the control loop starts
dev.write("SOURce1:APPLy:DC 1,1,0")
dev.write("SOURce2:APPLy:DC 1,1,0")
dev.write("OUTP1 ON")
dev.write("OUTP2 ON")

X, Y = 0.0, 0.0

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

    keys = pygame.key.get_pressed()
    
    # Flags to check if we actually need to send a USB command this frame
    update_x = False
    update_y = False

    if keys[pygame.K_w] and Y < 2:
        y -= 1
        Y -= 0.01
        update_y = True
    if keys[pygame.K_s] and Y > -2:
        y += 1
        Y += 0.01
        update_y = True
        
    # Fixed the boundary logic here (> -2 for moving left, < 2 for moving right)
    if keys[pygame.K_a] and X > -2:  
        x -= 1
        X -= 0.01
        update_x = True
    if keys[pygame.K_d] and X < 2:
        x += 1
        X += 0.01
        update_x = True

    # 2. UPDATE OFFSET ONLY
    # This alters the voltage without triggering a channel reset/relay click
    if update_y:
        dev.write(f"SOURce2:VOLTage:OFFSet {Y:.3f}")
    if update_x:
        dev.write(f"SOURce1:VOLTage:OFFSet {X:.3f}")

    screen.fill((0, 0, 0))
    pygame.draw.circle(screen, (255, 0, 0), (x, y), 5)  # Simulated laser dot
    pygame.display.flip()
    clock.tick(60)  # Run at 60 FPS

# Safe hardware cleanup on exit
dev.write("OUTP1 OFF")
dev.write("OUTP2 OFF")
dev.close()
pygame.quit()