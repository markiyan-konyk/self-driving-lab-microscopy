# A single tracked bead, in native camera-image pixels.
int32 id          # persistent track id (-1 if not yet linked)
float32 x         # image x, native pixels
float32 y         # image y, native pixels
float32 mass      # trackpy integrated brightness
float32 size      # trackpy radius of gyration (px)
float32 ecc       # trackpy eccentricity (0 = round)
