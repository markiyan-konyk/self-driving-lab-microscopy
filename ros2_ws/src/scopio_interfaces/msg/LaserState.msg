# Optical-tweezer (galvo laser) state. The laser is rigid to the objective, so
# its global position is the stage position plus the galvo deflection.
std_msgs/Header header
bool connected
float32 vx            # commanded volts, X mirror
float32 vy            # commanded volts, Y mirror
float32 home_vx       # zeroed home volts (set by ZeroTweezers)
float32 home_vy
float32 image_x       # computed laser spot, native image px
float32 image_y
float32 global_x      # laser global position, micrometres
float32 global_y
float32 global_z
