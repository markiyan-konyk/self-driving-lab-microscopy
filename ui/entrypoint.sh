#!/bin/bash
# Source ROS 2 and the interface workspace, then run the UI.
set -e
source /opt/ros/jazzy/setup.bash
source /iface_ws/install/setup.bash
exec "$@"
