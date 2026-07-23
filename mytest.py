import pyvisa
import time
import pygame


pygame.init()
screen = pygame.display.set_mode((400, 400))
pygame.display.set_caption("Galvo Controller")

# Initial galvo laser coordinates
x, y = 200, 200
speed = 5  # Pixels/units per tick

clock = pygame.time.Clock()

running = True
rm  = pyvisa.ResourceManager('@py')
dev = rm.open_resource('USB0::6833::1602::DG1ZA278M01038::0::INSTR')
dev.timeout = 20000
dev.query("*IDN?")
dev.write(f"SOURce1:APPLy:DC 1,1,0")
dev.write(f"OUTP1 ON")
X, Y = 0,0

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

    keys = pygame.key.get_pressed()
    if keys[pygame.K_w] and Y < 2:
        y -= 1
        Y -= 1/100
        dev.write(f"SOURce1:APPly:DC 1,1,{Y}")
    if keys[pygame.K_s] and Y > -2:
        y += 1
        Y += 1/100
        dev.write(f"SOURce1:APPly:DC 1,1,{Y}")
    if keys[pygame.K_a] and X < 2:
        x -= 1
        X -= 1/100
        dev.write(f"SOURce1:APPly:DC 1,1,{X}")
    if keys[pygame.K_d] and X > -2:
        x += 1
        X += 1/100
        dev.write(f"SOURce1:APPly:DC 1,1,{X}")

    screen.fill((0, 0, 0))
    pygame.draw.circle(screen, (255, 0, 0), (x, y), 5)  # Simulated laser dot
    pygame.display.flip()
    clock.tick(60)  # Run at 60

