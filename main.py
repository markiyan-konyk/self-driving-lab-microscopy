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
    camera.start_camera()

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
