# The microscope's spatial calibration, owned by calibration_node and published
# as a latched (transient-local) topic so late-joining clients get it
# immediately. Persisted to disk so it survives restarts.
#   um_per_px       : image scale, micrometres per pixel OF A FRAME
#                     um_per_px_width PIXELS WIDE
#   um_per_px_width : the frame width the scale was measured at.
#   um_per_px_window: the SENSOR window width it was measured through, i.e. how
#                     many sensor pixels that frame spanned.
#
#                     Both are needed because the camera switches sensor modes
#                     (camera/mode) and the two things a mode changes are
#                     independent. Binning changes how much slide one image pixel
#                     covers; CROPPING changes how much slide you see and leaves
#                     the scale alone. What um_per_px actually tracks is sensor
#                     pixels per image pixel = window / width, so:
#
#                       um_per_px_now = um_per_px * (window_now / width_now)
#                                                 / (um_per_px_window / um_per_px_width)
#
#                     On a Camera Module 2 the detail (1640 of 3280) and fast
#                     (640 of 1280) modes are both 2x binned -- same um_per_px,
#                     different field of view. Scaling by width alone would be
#                     wrong by 2.56x. 0 in either field means "measured before
#                     this existed": assume the current mode and do not convert.
#   steps_per_um_*  : stage conversion (Sangaboard steps per micrometre per axis)
std_msgs/Header header
bool has_um_per_px
float64 um_per_px
int32 um_per_px_width
int32 um_per_px_window
float64 steps_per_um_x
float64 steps_per_um_y
float64 steps_per_um_z
