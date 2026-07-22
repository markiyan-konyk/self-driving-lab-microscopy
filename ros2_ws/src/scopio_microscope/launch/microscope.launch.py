"""Bring up the full SCOPIO microscope sensor/effectuator graph.

    ros2 launch scopio_microscope microscope.launch.py

All driver nodes start under the /scopio namespace and load their parameters
from config/params.yaml. Each node degrades gracefully if its hardware is
absent, so the graph comes up even on an incomplete rig.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory("scopio_microscope")
    params = os.path.join(pkg_share, "config", "params.yaml")

    common = dict(package="scopio_microscope", namespace="scopio", output="screen",
                  parameters=[params])

    return LaunchDescription([
        Node(executable="calibration_node", name="calibration_node", **common),
        Node(executable="camera_node", name="camera_node", **common),
        Node(executable="stage_node", name="stage_node", **common),
        Node(executable="galvo_node", name="galvo_node", **common),
        Node(executable="temperature_node", name="temperature_node", **common),
        Node(executable="tracker_node", name="tracker_node", **common),
    ])
