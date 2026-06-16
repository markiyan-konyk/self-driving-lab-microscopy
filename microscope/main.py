"""Entry point: start the camera, the tracking worker, the motor controller and
the Flask server.

    python main.py

Module layout:
    camera.py            camera setup, live controls, calibration, recording
    controls.py          motor actuation + keyboard listener
    ML.py                circle detection + tracking worker + SSE
    authentification.py  password, session secret, login/logout
    client.py            Flask app and all HTTP routes
    frontend/            index.html, style.css, app.js, login.html
"""

import threading

from sangaboard import Sangaboard

import camera
import controls
import ML
import client


def main():
    try:
        camera.start_camera()
    except Exception as e:
        print(f"Camera failed to start: {e}")
        if e.__cause__ is not None:
            print(f"Caused by: {e.__cause__}")
        print()
        print("If the cause says 'Device or resource busy', another process is")
        print("holding the camera - usually a previous run of this program that")
        print("is still alive. On the Pi, find and stop it with:")
        print("    ps aux | grep -E 'main.py|libcamera|rpicam|motion'")
        print("    pkill -f main.py")
        print("or list the camera's users with:")
        print("    sudo fuser -v /dev/video* /dev/media*")
        print("then start this program again.")
        raise SystemExit(1)

    tracking_thread = threading.Thread(target=ML.tracking_worker, daemon=True)
    tracking_thread.start()

    try:
        with Sangaboard() as board:
            controls.sb = board
            controls.start_keyboard_listener()
            client.run_server()
    except Exception as e:
        controls.controller_error = str(e)
        print(f"Sangaboard unavailable: {controls.controller_error}")
        client.run_server()


if __name__ == "__main__":
    main()
