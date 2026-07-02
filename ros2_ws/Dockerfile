# SCOPIO microscope ROS 2 node, all-in-one container.
#
# Base: ROS 2 Jazzy (Ubuntu 24.04 Noble). The container owns the camera
# (picamera2/libcamera), the galvo (USB) and the stage (serial), so it runs
# --privileged with host networking and device passthrough (see compose).
#
# The libcamera/picamera2 apt packages must match the Pi's kernel/firmware --
# this is the one piece to verify on the Pi (see README "Camera caveat"). If the
# apt package is unavailable on your image, the camera node degrades gracefully
# (the rest of the graph still comes up) until it's sorted.
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

# NOTE: picamera2 / libcamera are NOT in Ubuntu's ARM repos -- they ship from
# Raspberry Pi OS (Debian). So they are intentionally NOT installed here: the
# camera node degrades gracefully without them (logs a warning, idles), and the
# rest of the graph (stage, galvo, calibration, tracker) comes up fine. To get
# live video, wire the camera in via one of the routes in README "Camera caveat"
# (e.g. run the camera node natively on the Pi, or base a camera image on Pi OS).

# Python deps for tracking + hardware drivers. --break-system-packages because
# Noble's Python is PEP-668 "externally managed"; fine inside a container.
# NOTE: no Flask here -- this image is the headless BACKEND. The web UI is a
# separate app (../ui) with its own image.
RUN pip3 install --no-cache-dir --break-system-packages \
        trackpy \
        pandas \
        scipy \
        simplejpeg \
        pyvisa \
        pyvisa-py \
        pyusb \
        libusb-package \
        sangaboard

# Build the workspace (interfaces + nodes) into the image.
WORKDIR /ros2_ws
COPY . /ros2_ws/
RUN source /opt/ros/jazzy/setup.bash && colcon build --symlink-install

# The reused driver code (microscope/, viscosity/) and recordings live in the
# repo, mounted at runtime; SCOPIO_REPO tells the nodes where to find it.
ENV SCOPIO_REPO=/workspace
ENV RMW_IMPLEMENTATION=rmw_fastrtps_cpp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["ros2", "launch", "scopio_microscope", "microscope.launch.py"]
