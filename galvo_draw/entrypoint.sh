#!/bin/bash
set -e
source /opt/ros/jazzy/setup.bash
source /iface_ws/install/setup.bash
exec "$@"
