# The microscope's global position, accumulated from relative moves (open-loop;
# origin = wherever the stage was at startup). This is what jump-scanning uses.
std_msgs/Header header
bool connected
int32 x          # steps
int32 y          # steps
int32 z          # steps
