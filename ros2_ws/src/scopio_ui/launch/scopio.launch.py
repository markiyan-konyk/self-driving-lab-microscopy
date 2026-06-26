"""Full SCOPIO bring-up: the four hardware driver nodes + the web UI gateway.

    ros2 launch scopio_ui scopio.launch.py

This is the default the Docker container runs. It includes the driver launch
from scopio_microscope and adds the gateway, so you get the sensor/effectuator
graph AND a browser UI (http://<pi>:8080), with the external decision computer
free to join the same graph over the network.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    drivers = os.path.join(
        get_package_share_directory("scopio_microscope"),
        "launch", "microscope.launch.py")
    return LaunchDescription([
        IncludeLaunchDescription(PythonLaunchDescriptionSource(drivers)),
        Node(package="scopio_ui", executable="ui_gateway", name="ui_gateway",
             namespace="scopio", output="screen"),
    ])
