"""
test_tracking.py
================
A quick *visual* check that the SiO2-bead tracking is detecting and linking well
and is properly calibrated -- run this BEFORE the full pipeline (main.py).

It opens a window and plays your clip back with every bead circled, centre-dotted
and labelled with its track id (exactly the overlay track.py draws), but writes
NO files -- no CSV, no video. It's purely for tuning the knobs by eye.

How to use
----------
1. Set VIDEO below to your clip.
2. Run it (`python test_tracking.py`) and watch: the circles should sit on the
   beads, ids should stay stable, with few missed or spurious detections.
3. Tune the DETECTION / LINKING knobs until it looks right, then copy the good
   values into track.py's DEFAULTS (and/or main.py) for the real run.

Controls:  SPACE = pause/resume,  Q or ESC = quit,
           while paused, any other key = step one frame.

No argparse on purpose -- just edit the CONFIG block and run it.
"""

import cv2

# Reuse track.py's heavy lifting so the preview matches the real run exactly.
from track import load_frames, detect, link, _id_color, DEFAULTS


# =============================== CONFIG =======================================
VIDEO = r"videos/rec1.mp4"     # <-- the clip you want to check (edit this)

# Detection / linking knobs -- same meaning as in track.py. These start from
# track.py's defaults; to tune one, just replace it with a literal, e.g.
#   DIAMETER = 13
DIAMETER     = DEFAULTS["diameter"]      # apparent bead size in px (MUST be odd)
MINMASS      = DEFAULTS["minmass"]       # min brightness to keep a detection
PERCENTILE   = DEFAULTS["percentile"]    # "auto" sizes it from frame-1 occupancy; or a number
CHANNEL      = DEFAULTS["channel"]       # BGR channel for detection: 0=B 1=G 2=R
INVERT       = DEFAULTS["invert"]        # True if beads are DARK on a bright bg
SEARCH_RANGE = DEFAULTS["search_range"]  # max px a bead may move between frames
MEMORY       = DEFAULTS["memory"]        # frames a bead may vanish and keep its id
WORKERS      = DEFAULTS["workers"]       # CPU processes for detection ("auto"=cores-1, 1=serial)

# Playback / display.
OUT_SCALE  = DEFAULTS["out_scale"]   # upscale the window so id labels are readable
DRAW_IDS   = True                    # draw the numeric track id next to each bead
MAX_FRAMES = 0                       # 0 = whole clip; e.g. 120 for a quick look
LOOP       = True                    # restart playback when it reaches the end
# ==============================================================================


def draw_overlay(canvas, group, radius, scale, draw_ids):
    """Circle + centre dot + id for every bead in this frame (mirrors track.py)."""
    if group is None:
        return
    for _, r in group.iterrows():
        x, y = int(round(r.x * scale)), int(round(r.y * scale))
        col = _id_color(r.particle)
        cv2.circle(canvas, (x, y), radius * scale, col, 1, cv2.LINE_AA)
        cv2.circle(canvas, (x, y), 1, (0, 255, 255), -1)  # yellow centre dot
        if draw_ids:
            cv2.putText(canvas, str(int(r.particle)),
                        (x + radius * scale, y - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.3 * scale / 2,
                        col, 1, cv2.LINE_AA)


def main():
    print(f"loading {VIDEO} ...")
    frames, fps = load_frames(VIDEO)
    if MAX_FRAMES:
        frames = frames[:MAX_FRAMES]
    print(f"  {len(frames)} frames, "
          f"{frames[0].shape[1]}x{frames[0].shape[0]}, {fps:.0f} fps")

    print("detecting beads per frame ...")
    feats = detect(frames, CHANNEL, DIAMETER, MINMASS, INVERT, PERCENTILE, WORKERS)
    print("linking into tracks ...")
    tracks = link(feats, SEARCH_RANGE, MEMORY)

    by_frame = {fr: g for fr, g in tracks.groupby("frame")}
    h, w = frames[0].shape[:2]
    radius = max(2, DIAMETER // 2)
    delay = max(1, int(1000 / (fps or 30)))    # ms per frame -> ~real-time
    win = "tracking preview  (SPACE pause / Q-ESC quit)"

    print("playing ...  SPACE = pause,  Q/ESC = quit,  (paused) any key = step")
    paused, i = False, 0
    try:
        while True:
            canvas = cv2.resize(frames[i], (w * OUT_SCALE, h * OUT_SCALE),
                                interpolation=cv2.INTER_LINEAR)
            g = by_frame.get(i)
            draw_overlay(canvas, g, radius, OUT_SCALE, DRAW_IDS)
            n = 0 if g is None else len(g)
            cv2.putText(canvas, f"frame {i}   balls {n}", (8, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.imshow(win, canvas)

            key = cv2.waitKey(0 if paused else delay) & 0xFF
            if key in (ord('q'), 27):           # Q or ESC
                break
            if key == ord(' '):                 # toggle pause
                paused = not paused
                continue
            if paused:                          # step one frame while paused
                i = min(i + 1, len(frames) - 1)
                continue

            i += 1
            if i >= len(frames):
                if not LOOP:
                    break
                i = 0

            # honour the window's X button
            if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                break
    except cv2.error as e:
        print("\n[!] OpenCV could not open a display window:\n   ", e)
        print("    This usually means the headless OpenCV build is installed.")
        print("    Fix with:  pip uninstall opencv-python-headless")
        print("               pip install opencv-python")
    finally:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
