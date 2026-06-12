"""Motor actuation (Sangaboard) and physical keyboard input listening.

The Sangaboard handle (``sb``) is assigned from ``main`` once the board is
opened. Helpers read it as a module global, so always set it via
``controls.sb = board`` (not ``from controls import sb``) for the update to be
visible here.
"""

import threading

try:
    from pynput import keyboard
    KEYBOARD_IMPORT_ERROR = None
except Exception as exc:
    keyboard = None
    KEYBOARD_IMPORT_ERROR = exc

# ========== Motor state ==========
sb = None
controller_error = None
move_lock = threading.Lock()

steps = {"x": 40, "y": 40, "z": 40}


# ========== Stage Wrapper for Sangaboard ==========
class SangaboardWrapper:
    """Provides .position dict, .move_rel(), .move_abs() for autofocus."""

    def __init__(self, board):
        self._board = board
        self._pos = {"x": 0, "y": 0, "z": 0}
        # FIX #4: bare except 제거 → 명시적 Exception 캐치 + dict 타입 강제
        try:
            if hasattr(board, "position"):
                raw = board.position
                if isinstance(raw, dict):
                    self._pos = {k: int(raw.get(k, 0)) for k in ("x", "y", "z")}
                elif hasattr(raw, "__iter__"):
                    vals = list(raw)
                    self._pos = {"x": int(vals[0]), "y": int(vals[1]), "z": int(vals[2])}
        except Exception as exc:
            print(f"[SangaboardWrapper] Could not read initial position: {exc}")

    @property
    def position(self):
        return dict(self._pos)

    def move_rel(self, delta):
        with move_lock:
            self._board.move_rel(delta)
            for axis, d in delta.items():
                if axis in self._pos:
                    self._pos[axis] += d

    def move_abs(self, target):
        delta = {
            "x": target["x"] - self._pos["x"],
            "y": target["y"] - self._pos["y"],
            "z": target["z"] - self._pos["z"],
        }
        self.move_rel(delta)


def move_motor(direction):
    """Jog the stage in ``direction``. Returns a (body, status_code) tuple."""
    dir_map = {
        "left":      [ steps["x"],  0,           0],
        "right":     [-steps["x"],  0,           0],
        "up":        [ 0,           steps["y"],  0],
        "down":      [ 0,          -steps["y"],  0],
        "page_up":   [ 0,           0,           steps["z"]],
        "page_down": [ 0,           0,          -steps["z"]],
    }
    if direction not in dir_map:
        return "Unknown direction", 404
    if sb is None:
        return "Sangaboard unavailable", 503
    with move_lock:
        sb.move_rel(dir_map[direction])
    return "OK", 200


def adjust_step(axis, op):
    """Increase/decrease the step size for an axis. Returns (body, status)."""
    if axis not in steps:
        return "Unknown axis", 404
    if op == "inc":
        steps[axis] += 5
    elif op == "dec":
        steps[axis] = max(1, steps[axis] - 5)
    else:
        return "Unknown op", 400
    return str(steps[axis]), 200


def start_keyboard_listener():
    if keyboard is None:
        print(f"Keyboard disabled: {KEYBOARD_IMPORT_ERROR}")
        return None

    pressed_keys = set()

    def rebuild_key_map():
        return {
            keyboard.Key.right:     [-steps["x"],  0,           0],
            keyboard.Key.left:      [ steps["x"],  0,           0],
            keyboard.Key.up:        [ 0,           steps["y"],  0],
            keyboard.Key.down:      [ 0,          -steps["y"],  0],
            keyboard.Key.page_up:   [ 0,           0,           steps["z"]],
            keyboard.Key.page_down: [ 0,           0,          -steps["z"]],
        }

    key_map_move = rebuild_key_map()

    def on_press(key):
        nonlocal key_map_move
        with move_lock:
            if key in key_map_move and sb is not None:
                sb.move_rel(key_map_move[key])
            if hasattr(key, "char") and key.char in ("x", "y", "z", "=", "-"):
                pressed_keys.add(key.char)
            adjusted = False
            for axis in ("x", "y", "z"):
                if axis in pressed_keys:
                    if "=" in pressed_keys:
                        steps[axis] += 5
                        adjusted = True
                    if "-" in pressed_keys:
                        steps[axis] = max(1, steps[axis] - 5)
                        adjusted = True
            if adjusted:
                key_map_move = rebuild_key_map()
                if hasattr(key, "char") and key.char in ("=", "-"):
                    pressed_keys.discard(key.char)

    def on_release(key):
        if hasattr(key, "char") and key.char in ("x", "y", "z", "=", "-"):
            pressed_keys.discard(key.char)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    return listener
