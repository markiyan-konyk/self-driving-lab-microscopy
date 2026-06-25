# Recording state of the camera.
std_msgs/Header header
bool recording
float32 duration_s     # requested duration; <= 0 means "until stopped"
float32 remaining_s    # seconds left (NaN/0 if infinite or idle)
string filename
