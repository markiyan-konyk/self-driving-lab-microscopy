Traceback (most recent call last):
  File "/usr/lib/python3/dist-packages/picamera2/picamera2.py", line 356, in __init__
    self._open_camera()
    ~~~~~~~~~~~~~~~~~^^
  File "/usr/lib/python3/dist-packages/picamera2/picamera2.py", line 574, in _open_camera
    self.camera.acquire()
    ~~~~~~~~~~~~~~~~~~~^^
RuntimeError: Failed to acquire camera: Device or resource busy

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/home/min/self-driving-lab/software/main.py", line 43, in <module>
    main()
    ~~~~^^
  File "/home/min/self-driving-lab/software/main.py", line 26, in main
    camera.start_camera()
    ~~~~~~~~~~~~~~~~~~~^^
  File "/home/min/self-driving-lab/software/camera.py", line 144, in start_camera
    _start_camera_unlocked()
    ~~~~~~~~~~~~~~~~~~~~~~^^
  File "/home/min/self-driving-lab/software/camera.py", line 125, in _start_camera_unlocked
    picam2 = Picamera2()
  File "/usr/lib/python3/dist-packages/picamera2/picamera2.py", line 368, in __init__
    raise RuntimeError("Camera __init__ sequence did not complete.") from e
RuntimeError: Camera __init__ sequence did not complete.
