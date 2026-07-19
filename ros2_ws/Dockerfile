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
        libusb-1.0-0 \
        ros-jazzy-sensor-msgs \
        ros-jazzy-std-srvs \
    && rm -rf /var/lib/apt/lists/*
# libusb-1.0-0 is REQUIRED for pyvisa-py/pyusb to enumerate the USB-TMC AWG (the
# Rigol galvo). Without it the container sees only serial ports and galvo_node
# reports "AWG unavailable" -- the host has libusb so galvo_tests work there, but
# the Ubuntu container needs it installed explicitly.

# NOTE: picamera2 / libcamera are NOT in Ubuntu's ARM repos -- they ship from
# Raspberry Pi OS (Debian). So they are intentionally NOT installed here. The
# camera is owned by the separate `camera` compose service (camera_server/,
# Debian bookworm + the Raspberry Pi apt archive) and camera_node falls back to
# BRIDGE MODE: it ingests the camera server's MJPEG stream over loopback HTTP,
# republishes frames on image/compressed, and forwards the camera services to
# the camera server's HTTP API. See camera_server/README.md.

# Python deps for tracking + hardware drivers. --break-system-packages because
# Noble's Python is PEP-668 "externally managed"; fine inside a container.
# NOTE: no Flask here -- this image is the headless BACKEND. The web UI is a
# separate app (../ui) that talks to the gateway over plain HTTP.
# trackpy/pandas/scipy (the bead-TRACKER deps) are intentionally omitted: on the
# Pi they pull a newer numpy that pip can't swap in over the apt-managed numpy
# (it fails to uninstall the debian numpy). tracker_node degrades gracefully
# without them. Re-add them later with a proper numpy strategy (e.g. a venv, or
# a pinned numpy) when you actually wire up tracking.
RUN pip3 install --no-cache-dir --break-system-packages \
        simplejpeg \
        pyvisa \
        pyvisa-py \
        pyusb \
        libusb-package \
        sangaboard \
        fastapi \
        "uvicorn[standard]" \
        httpx
# fastapi/uvicorn/httpx power the scopio_gateway package (the HTTP/WebSocket
# API that external clients use instead of joining the DDS graph). They are
# pure-python and light -- fine on the Pi.

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
