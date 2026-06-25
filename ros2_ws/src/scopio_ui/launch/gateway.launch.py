"""Launch just the UI gateway (a ROS client) in the /scopio namespace.

    ros2 launch scopio_ui gateway.launch.py

Use this when the driver nodes are already running (e.g. started separately or
on another machine) and you only want to add the web UI.
"""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        Node(package="scopio_ui", executable="ui_gateway", name="ui_gateway",
             namespace="scopio", output="screen"),
    ])
