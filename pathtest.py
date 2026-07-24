"""
pathtest.py -- optical-tweezer bead sorting: path planner + pygame simulation.

Moves beads (SiO2, ~11 px across in the 640x480 microscope frame) from wherever
they are to a set of target positions, one bead at a time, obeying the galvo
hardware constraints:

  * only horizontal OR vertical moves (X = Rigol CH1, Y = CH2) -- no diagonals
  * every channel switch costs a relay dwell, so the planner minimises switches
  * dragging must be SLOW (the bead has to stay in the potential well)
  * the trap cannot be switched off: the only way to release a bead is to move
    away FAST, and the only way to reach the next bead is a fast jump ("teleport")

The loop is closed: after each bead is placed, the bead positions are read again
(here from the simulation, on the microscope from the tracker) and the planner
picks the next bead from scratch.

The beam position is sent to the DG1022Z as it moves: CH1 = x, CH2 = y, DC
offset only. Writes are capped at WRITE_HZ and only ever go to the one axis that
is currently moving, so the instrument stays far below its command budget.

Run:  python pathtest.py         drives the wavegen (default)
      python pathtest.py --sim   simulation only, no instrument
      [SPACE] fast-forward   [R] reset   [ESC] quit
"""

import argparse
import heapq
import math
import random
import time
from collections import namedtuple

import numpy as np
import pygame

from DG1022Z import DG1022Z

# --- scene, in microscope pixels (measured from viscosity/videos/rec1.mp4) ---
W, H = 640, 480
BEAD_R = 5.5            # bead radius; trackpy diameter=11 on this footage
LASER_R = BEAD_R        # beam waist assumed equal to a bead
TRAP_R = 9.0            # a bead closer than this to the beam centre is trapped
CLEAR_R = 26.0          # planner keeps the beam this far from every other bead
EDGE = 14.0             # keep the beam inside the field of view

# --- motion ---
DRAG_SPEED = 60.0       # px/s, slow enough that the bead stays in the well
FAST_SPEED = 2500.0     # px/s, galvo slew: releases a bead / jumps to the next
BEAD_FOLLOW = 80.0      # px/s, how fast a trapped bead can chase the beam
SWITCH_DELAY = 0.12     # s, relay dwell every time we change channel
PLACED_TOL = 3.0        # px, close enough to count as placed

# --- planner ---
GRID = 4.0              # routing grid, px
TURN_COST = 120.0       # a channel switch costs this many px of travel
CROWD_R = 160.0         # targets with neighbours inside this radius ...
CROWD_W = 90.0          # ... get this bonus each, so tight spots are filled first
ESCAPE_D = 70.0         # how far to slew away to drop a bead

# --- hardware ---
VOLT = 2.0              # DC output range is -VOLT..+VOLT and spans the whole frame
WRITE_HZ = 30.0         # cap on DC writes per second (the DG1022Z tolerates ~100)
WRITE_STEP = 0.3        # px; smaller moves than this are not worth a command

Move = namedtuple("Move", "axis value fast")   # axis 'x'|'y' -> CH1|CH2


# =============================================================================
#  Planner -- pure geometry, no pygame. This is what the real galvo code reuses.
# =============================================================================

def _blocked_mask(obstacles):
    """Boolean grid: True where the beam may not go (too near a bead, or edge)."""
    nx, ny = int(W / GRID), int(H / GRID)
    gx, gy = np.meshgrid((np.arange(nx) + 0.5) * GRID,
                         (np.arange(ny) + 0.5) * GRID, indexing="ij")
    blocked = np.zeros((nx, ny), bool)
    for ox, oy in obstacles:
        blocked |= (gx - ox) ** 2 + (gy - oy) ** 2 < CLEAR_R ** 2
    m = int(math.ceil(EDGE / GRID))
    blocked[:m] = blocked[-m:] = True
    blocked[:, :m] = blocked[:, -m:] = True
    return blocked


def _cell(p):
    return (min(int(W / GRID) - 1, max(0, int(p[0] / GRID))),
            min(int(H / GRID) - 1, max(0, int(p[1] / GRID))))


def _merge(moves):
    """Collapse runs on the same axis -- only the last value of a run matters."""
    out = []
    for m in moves:
        if out and out[-1].axis == m.axis and out[-1].fast == m.fast:
            out[-1] = m
        else:
            out.append(m)
    return out


def _to_moves(points, start, fast):
    """Turn a polyline into single-axis moves starting from `start`."""
    moves, cur = [], list(start)
    for x, y in points:
        if abs(x - cur[0]) > 1e-6:
            moves.append(Move("x", x, fast)); cur[0] = x
        if abs(y - cur[1]) > 1e-6:
            moves.append(Move("y", y, fast)); cur[1] = y
    return _merge(moves)


def route(start, goal, blocked):
    """Cheapest rectilinear path start->goal over free cells.

    Dijkstra over (cell, current axis): stepping costs distance, changing axis
    costs TURN_COST, so the result naturally uses as few channel switches as it
    can get away with. Returns a list of Moves, or None if there is no way through.
    """
    nx, ny = blocked.shape
    si, sj = _cell(start)
    gi, gj = _cell(goal)
    if blocked[gi, gj]:
        return None

    best = {(si, sj, 0): 0.0, (si, sj, 1): 0.0}
    prev, seen = {}, set()
    pq = [(0.0, si, sj, 0), (0.0, si, sj, 1)]
    while pq:
        d, i, j, a = heapq.heappop(pq)
        if (i, j, a) in seen:
            continue
        seen.add((i, j, a))
        if (i, j) == (gi, gj):
            chain, k = [], (i, j, a)
            while k in prev:
                chain.append(k); k = prev[k]
            chain.append(k)
            pts = [((ci + 0.5) * GRID, (cj + 0.5) * GRID) for ci, cj, _ in reversed(chain)]
            return _to_moves(pts + [goal], start, fast=False)

        cand = [(i, j, 1 - a, d + TURN_COST)]
        ni, nj = (i + 1, j) if a == 0 else (i, j + 1)
        pi, pj = (i - 1, j) if a == 0 else (i, j - 1)
        for ci, cj in ((ni, nj), (pi, pj)):
            if 0 <= ci < nx and 0 <= cj < ny and not blocked[ci, cj]:
                cand.append((ci, cj, a, d + GRID))
        for ci, cj, ca, cd in cand:
            if cd < best.get((ci, cj, ca), math.inf):
                best[(ci, cj, ca)] = cd
                prev[(ci, cj, ca)] = (i, j, a)
                heapq.heappush(pq, (cd, ci, cj, ca))
    return None


def cost(moves, axis=None):
    """Travel distance plus the penalty for every channel switch."""
    total, pos = 0.0, None
    for m in moves:
        if m.axis != axis:
            total += TURN_COST
            axis = m.axis
        total += 0.0 if pos is None else abs(m.value - pos)
        pos = m.value
    return total


def escape(pos, obstacles, last_axis):
    """One fast move that yanks the beam out of the well it just filled."""
    options = []
    for axis in ("x", "y"):
        for sign in (-1, 1):
            k = 0 if axis == "x" else 1
            val = pos[k] + sign * ESCAPE_D
            if not EDGE < val < (W if k == 0 else H) - EDGE:
                continue
            end = (val, pos[1]) if k == 0 else (pos[0], val)
            room = min((math.dist(end, o) for o in obstacles), default=1e9)
            options.append((room - (0 if axis == last_axis else TURN_COST), axis, val))
    if not options:
        return []
    _, axis, val = max(options)
    return [Move(axis, val, True)]


def assign(beads, targets):
    """Greedy nearest-first pairing of beads to targets (rectilinear distance)."""
    pairs = sorted(((abs(b[0] - t[0]) + abs(b[1] - t[1]), bi, ti)
                    for bi, b in enumerate(beads)
                    for ti, t in enumerate(targets)))
    bead_of, used_b, used_t = {}, set(), set()
    for _, bi, ti in pairs:
        if bi not in used_b and ti not in used_t:
            bead_of[ti] = bi
            used_b.add(bi); used_t.add(ti)
    return bead_of


def plan_next(beads, targets, laser):
    """Pick the bead to move now and return the full move list for it.

    Called fresh after every placement, on re-read bead positions. Among all
    routable beads we take the cheapest path, minus a bonus for targets that sit
    in a crowded area -- those are the ones that become unreachable once their
    neighbours are filled, so they are done while there is still room.

    Returns (bead_index, moves) or (None, None) when nothing is left to do.
    """
    bead_of = assign(beads, targets)
    todo = [(ti, bi) for ti, bi in bead_of.items()
            if math.dist(beads[bi], targets[ti]) > PLACED_TOL]
    best = None
    for ti, bi in todo:
        others = [b for k, b in enumerate(beads) if k != bi]
        drag = route(beads[bi], targets[ti], _blocked_mask(others))
        if drag is None:
            continue
        # Jump onto the bead; order the two axes so the first drag axis is
        # already selected when the drag starts -> one switch less.
        first = drag[0].axis
        jump = [Move("y", beads[bi][1], True), Move("x", beads[bi][0], True)]
        if first == "y":
            jump.reverse()
        moves = _merge(jump + drag)
        moves += escape(targets[ti], others, drag[-1].axis)
        crowd = sum(1 for tj, _ in todo
                    if tj != ti and math.dist(targets[ti], targets[tj]) < CROWD_R)
        score = cost(moves) - CROWD_W * crowd
        if best is None or score < best[0]:
            best = (score, bi, moves)
    return (best[1], best[2]) if best else (None, None)


# =============================================================================
#  Executor -- walks a move list. Swap this for the DG1022Z on real hardware.
# =============================================================================

class Executor:
    def __init__(self):
        self.moves, self.i, self.axis, self.wait, self.switches = [], 0, None, 0.0, 0

    @property
    def busy(self):
        return self.i < len(self.moves)

    def load(self, moves):
        self.moves, self.i = moves, 0

    def step(self, pos, dt):
        if not self.busy:
            return pos
        if self.wait > 0:                       # relay is switching, hold still
            self.wait -= dt
            return pos
        m = self.moves[self.i]
        if m.axis != self.axis:
            self.axis, self.wait = m.axis, SWITCH_DELAY
            self.switches += 1
            return pos
        k = 0 if m.axis == "x" else 1
        p = list(pos)
        d = m.value - p[k]
        p[k] += math.copysign(min(abs(d), (FAST_SPEED if m.fast else DRAG_SPEED) * dt), d)
        if abs(m.value - p[k]) < 1e-3:
            p[k] = m.value
            self.i += 1
        return tuple(p)


# =============================================================================
#  Galvo -- mirrors the simulated beam onto the real wavegen (optional)
# =============================================================================

def to_volts(axis, px):
    """Frame pixels -> DC volts. Each axis uses the full -VOLT..+VOLT swing."""
    return (px / (W if axis == "x" else H) * 2.0 - 1.0) * VOLT


# =============================================================================
#  Simulation
# =============================================================================

def spawn_beads(n, rng, sep=45.0):
    beads = []
    while len(beads) < n:
        p = (rng.uniform(60, W - 60), rng.uniform(60, H - 60))
        if all(math.dist(p, q) > sep for q in beads):
            beads.append(p)
    return beads


def grid_targets(cols=3, rows=3, pitch=110):
    ox, oy = W / 2 - pitch * (cols - 1) / 2, H / 2 - pitch * (rows - 1) / 2
    return [(ox + c * pitch, oy + r * pitch) for r in range(rows) for c in range(cols)]


def pull_beads(beads, laser, dt):
    """The only force in the sim: anything inside TRAP_R is pulled to the beam.

    Slow beam -> the bead keeps up and is dragged. Fast beam -> the beam is out
    of TRAP_R within a frame and the bead is left behind. Same rule, both cases.
    """
    for i, b in enumerate(beads):
        d = math.dist(b, laser)
        if 1e-6 < d <= TRAP_R:
            s = min(BEAD_FOLLOW * dt, d)
            beads[i] = (b[0] + (laser[0] - b[0]) / d * s,
                        b[1] + (laser[1] - b[1]) / d * s)


def move_points(start, moves):
    pts, p = [start], list(start)
    for m in moves:
        p[0 if m.axis == "x" else 1] = m.value
        pts.append(tuple(p))
    return pts


SCALE = 1.6
BG = (10, 26, 30)
C_BEAD = (150, 230, 225)
C_HELD = (255, 225, 120)
C_TARGET = (70, 110, 115)
C_SLOW = (235, 90, 70)
C_FAST = (90, 90, 120)
C_TEXT = (190, 215, 215)


def to_screen(p):
    return int(p[0] * SCALE), int(p[1] * SCALE)


def main(sim_only=False, hz=WRITE_HZ):
    dg = None
    if not sim_only:
        dg = DG1022Z()
        dg._open()
        print("resource :", dg.resource or "(auto)")
        print("IDN      :", dg.device.query("*IDN?").strip())
        dg.dcinit()
        print("outputs on, DC mode. CH1 = x, CH2 = y")

    pygame.init()
    screen = pygame.display.set_mode((int(W * SCALE), int(H * SCALE)))
    pygame.display.set_caption("optical tweezer path test")
    font = pygame.font.SysFont("consolas", 15)
    clock = pygame.time.Clock()

    rng = random.Random()
    beads = spawn_beads(9, rng)
    targets = grid_targets()
    laser = (EDGE, EDGE)
    ex = Executor()
    held, cycle, plan, trail = None, 0, [], []
    running = True

    # park the beam at the start position: one channel, then the other with a
    # relay dwell in between. From here on only ONE axis ever moves at a time.
    sent, last_k = list(laser), 1
    if dg:
        dg.dcupdate(1, to_volts("x", laser[0]))
        time.sleep(SWITCH_DELAY)
        dg.dcupdate(2, to_volts("y", laser[1]))
    writes, err, t_check = 0, "", 0.0
    t_last = t0 = time.monotonic()

    try:
        while running:
            dt = clock.tick(60) / 1000.0
            for e in pygame.event.get():
                if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                    running = False
                elif e.type == pygame.KEYDOWN and e.key == pygame.K_r:
                    beads, laser, ex = spawn_beads(9, rng), (EDGE, EDGE), Executor()
                    held, cycle, plan, trail = None, 0, [], []
            # fast-forward is simulation-only: with the galvo attached the beam
            # has to move in real time or it is not the same test.
            if pygame.key.get_pressed()[pygame.K_SPACE] and not dg:
                dt *= 6

            if not ex.busy:
                # closed loop: re-read every bead position, then plan the next one
                held, moves = plan_next(list(beads), targets, laser)
                if moves:
                    ex.load(moves)
                    plan = move_points(laser, moves)
                    cycle += 1
                else:
                    held = None

            dt = min(dt, 0.05)
            laser = ex.step(laser, dt)
            pull_beads(beads, laser, dt)
            trail.append(laser)
            trail = trail[-260:]

            # --- send the beam to the galvo -----------------------------------
            # One command per tick at most, so the rate is hard-capped at `hz`.
            # Only the axis the plan is moving differs from `sent`, and the first
            # command on a different channel waits out SWITCH_DELAY since the
            # last one, measured from the write itself -- that is the relay.
            now = time.monotonic()
            if dg and now - t_last >= 1.0 / hz:
                for k in (0, 1):
                    if abs(laser[k] - sent[k]) > WRITE_STEP:
                        if k != last_k and now - t_last < SWITCH_DELAY:
                            break                      # relay has not settled
                        dg.dcupdate(k + 1, to_volts("xy"[k], laser[k]))
                        sent[k], last_k, t_last = laser[k], k, now
                        writes += 1
                        break
            # a clean error queue is the proof nothing is backing up; ask only
            # while the beam is parked mid-switch, so the round trip costs nothing
            if dg and ex.wait > 0 and now >= t_check:
                err = dg.device.query(":SYSTem:ERRor?").strip()
                t_check = now + 1.0

            screen.fill(BG)
            for t in targets:
                pygame.draw.circle(screen, C_TARGET, to_screen(t), int(BEAD_R * SCALE) + 3, 1)
            if plan:
                for i, m in enumerate(ex.moves):
                    col = C_FAST if m.fast else C_SLOW
                    pygame.draw.line(screen, col, to_screen(plan[i]), to_screen(plan[i + 1]),
                                     1 if m.fast else 2)
            if len(trail) > 1:
                pygame.draw.lines(screen, (40, 70, 75), False, [to_screen(p) for p in trail], 1)
            for i, b in enumerate(beads):
                pygame.draw.circle(screen, C_HELD if i == held else C_BEAD,
                                   to_screen(b), max(2, int(BEAD_R * SCALE)))
            pygame.draw.circle(screen, (255, 60, 40), to_screen(laser), int(LASER_R * SCALE), 1)
            pygame.draw.circle(screen, (255, 60, 40), to_screen(laser), int(TRAP_R * SCALE), 1)

            left = sum(1 for t in targets
                       if min(math.dist(b, t) for b in beads) > PLACED_TOL)
            hud = [f"cycle {cycle}   bead {held if held is not None else '-'}   remaining {left}",
                   f"channel {ex.axis or '-'}   switches {ex.switches}"
                   f"   move {min(ex.i + 1, len(ex.moves))}/{len(ex.moves)}",
                   f"beam  {to_volts('x', laser[0]):+.3f} V  {to_volts('y', laser[1]):+.3f} V"]
            if dg:
                hud.append(f"galvo {writes} writes  {writes / max(now - t0, 1e-9):.1f}/s  {err}")
            hud.append("SPACE fast-forward   R reset   ESC quit")
            for i, line in enumerate(hud):
                screen.blit(font.render(line, True, C_TEXT), (10, 8 + i * 18))
            pygame.display.flip()
    finally:
        if dg:
            dg._close()
            print(f"{writes} writes in {time.monotonic() - t0:.0f} s, outputs off")
        pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="optical tweezer path test")
    ap.add_argument("--sim", action="store_true",
                    help="simulation only, do not touch the wavegen")
    ap.add_argument("--hz", type=float, default=WRITE_HZ,
                    help=f"max DC writes per second (default {WRITE_HZ:.0f})")
    args = ap.parse_args()
    main(args.sim, args.hz)
