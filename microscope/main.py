"""Entry point: start the camera, the display worker, the motor controller and
the Flask server.

    python main.py

Module layout:
    camera.py            camera setup, live controls, calibration, recording,
                         and the live JPEG display worker
    controls.py          motor actuation + keyboard listener
    authentification.py  password, session secret, login/logout
    client.py            Flask app and all HTTP routes
    frontend/            index.html, style.css, app.js, login.html
"""

import os
import threading

from sangaboard import Sangaboard

import camera
import controls
import client


def connect_galvo():
    """Open the optical-tweezer galvo (Rigol DG1022Z) if a resource is given.

    The address comes from the GALVO_RESOURCE env var (find it with
    galvosetup.py). Failure is non-fatal: the app runs fine with no AWG
    attached -- the laser controls just report 'unavailable'.
    """
    resource = os.environ.get("GALVO_RESOURCE")
    if not resource:
        print("GALVO_RESOURCE not set; optical tweezer disabled.")
        return None
    try:
        import galvo
        return galvo.Galvo(resource)
    except Exception as e:
        print(f"Galvo unavailable: {e}")
        return None


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

    display_thread = threading.Thread(target=camera.display_worker, daemon=True)
    display_thread.start()

    client.galvo = connect_galvo()

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
