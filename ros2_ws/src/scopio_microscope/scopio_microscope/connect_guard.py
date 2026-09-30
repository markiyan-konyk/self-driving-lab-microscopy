"""Bound an instrument connect attempt in time.

WHY: opening a USB-TMC instrument through pyvisa-py can HANG -- not time out,
hang -- inside libusb. It is what happens on this Pi when the kernel's usbtmc
driver owns the TC10 and pyvisa-py tries to take it over (tc10_read.py calls it
"HUNG, abandoned"). The VISA timeout does not cover it: it governs reads and
writes, not the open. A node that connects on its own executor thread then
freezes for good -- the retry timer never fires again, the status topic stops,
and nothing says why. That is indistinguishable from "the instrument is gone".

So a connect attempt runs on a worker thread with a deadline. Past it the
attempt is ABANDONED (a thread stuck in C code cannot be killed) and the node
carries on, reporting the hang. While the abandoned attempt is still alive,
every later attempt is refused immediately: a second libusb session on a device
the first one is stuck on only makes it worse. If the abandoned attempt finishes
late after all, whatever it opened is handed to `discard` and closed.

Standard library only, like the drivers.
"""

import threading


class ConnectHung(Exception):
    """A connect attempt blew its deadline, or an earlier one is still stuck."""


class ConnectGuard:
    def __init__(self, deadline_s):
        self.deadline_s = float(deadline_s)
        self._lock = threading.Lock()
        self._hung = None          # the abandoned worker thread, while alive

    def hung(self):
        """True while an abandoned attempt is still stuck."""
        with self._lock:
            return self._hung is not None and self._hung.is_alive()

    def run(self, attempt, discard):
        """Return attempt()'s result, re-raise its exception, or raise ConnectHung.

        attempt() must clean up after itself when it RAISES. discard(result) is
        called only for an attempt that succeeded after it was abandoned.
        """
        if self.hung():
            raise ConnectHung(
                "an earlier connect attempt is still stuck inside the USB stack; "
                "not starting another on the same device. Unplug and replug the "
                "instrument (or restart the container) to free it.")

        box = {}
        done = threading.Event()
        gate = threading.Lock()
        abandoned = [False]

        def work():
            try:
                box["result"] = attempt()
            except BaseException as exc:          # delivered to the caller below
                box["error"] = exc
            with gate:
                late = abandoned[0]
                done.set()
            if late and "result" in box:
                try:
                    discard(box["result"])
                except Exception:
                    pass

        worker = threading.Thread(target=work, daemon=True, name="instrument-connect")
        worker.start()
        if not done.wait(self.deadline_s):
            with gate:
                if not done.is_set():             # still running: give up on it
                    abandoned[0] = True
                    with self._lock:
                        self._hung = worker
                    raise ConnectHung(
                        f"connect did not return within {self.deadline_s:.0f} s -- "
                        "stuck inside the USB stack (on this Pi: pyvisa-py fighting "
                        "the kernel usbtmc driver for the device). Abandoned it.")
        if "error" in box:
            raise box["error"]
        return box["result"]
