# The microscope's spatial calibration, owned by calibration_node and published
# as a latched (transient-local) topic so late-joining clients get it
# immediately. Persisted to disk so it survives restarts.
#   um_per_px       : image scale, micrometres per pixel OF A FRAME
#                     um_per_px_width PIXELS WIDE
#   um_per_px_width : the frame width the scale was measured at. The camera can
#                     switch sensor modes (see camera/mode), and the same slide
#                     then lands on a different number of pixels, so a bare
#                     um_per_px is only meaningful next to the width it was
#                     taken at. Convert for the frame you actually have:
#                         um_per_px_now = um_per_px * um_per_px_width / width_now
#                     0 means "measured before this field existed"; treat it as
#                     the current width, i.e. do not rescale.
#   steps_per_um_*  : stage conversion (Sangaboard steps per micrometre per axis)
std_msgs/Header header
bool has_um_per_px
float64 um_per_px
int32 um_per_px_width
float64 steps_per_um_x
float64 steps_per_um_y
float64 steps_per_um_z
