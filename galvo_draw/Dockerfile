# galvo_draw - standalone ROS 2 client (laser vector-drawing app).
# Build context is the REPO ROOT so it can build the frozen interfaces it needs.
#   cd galvo_draw && docker compose up --build
FROM ros:jazzy-ros-base
SHELL ["/bin/bash", "-lc"]

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip python3-colcon-common-extensions python3-numpy \
        ros-jazzy-sensor-msgs ros-jazzy-std-srvs \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages flask

# Build only the frozen interfaces so this app can call awg/write.
WORKDIR /iface_ws
COPY ros2_ws/src/scopio_interfaces /iface_ws/src/scopio_interfaces
RUN source /opt/ros/jazzy/setup.bash && colcon build

COPY galvo_draw /app
WORKDIR /app
ENV RMW_IMPLEMENTATION=rmw_fastrtps_cpp
COPY galvo_draw/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "app.py"]
