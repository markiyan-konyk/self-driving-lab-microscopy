# SCOPIO Web UI - standalone ROS 2 client image.
#
# Build context is the REPO ROOT (see docker-compose.yml) so this image can pull
# in the frozen interface package (ros2_ws/src/scopio_interfaces) and build just
# that -- the UI needs the message/service definitions to speak to the backend,
# but it builds NO driver nodes and owns NO hardware.
#
#   cd ui && docker compose up --build
FROM ros:jazzy-ros-base

SHELL ["/bin/bash", "-lc"]

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-colcon-common-extensions \
        python3-opencv \
        ffmpeg \
        ros-jazzy-sensor-msgs \
        ros-jazzy-std-srvs \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages flask simplejpeg

# Build ONLY the frozen interfaces, so the UI can speak the contract.
WORKDIR /iface_ws
COPY ros2_ws/src/scopio_interfaces /iface_ws/src/scopio_interfaces
RUN source /opt/ros/jazzy/setup.bash && colcon build

# The UI application itself (run_ui.py, galvo_geometry.py, frontend/).
COPY ui /ui
WORKDIR /ui
ENV RMW_IMPLEMENTATION=rmw_fastrtps_cpp

COPY ui/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "run_ui.py"]
