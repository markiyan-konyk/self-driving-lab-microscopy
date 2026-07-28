"""temperature_node - the sample temperature controller, exposed whole.

Same pattern as galvo_node: the node owns the session to a Wavelength
Electronics TC10 LAB through the vendored, EDITABLE `drivers/TC10LAB.TC10LAB`
class, and offers every public method of it over ONE service.

  srv  temperature/call    scopio_interfaces/InstrumentCall
  pub  temperature/status  scopio_interfaces/TemperatureStatus  (polled)

Add a method to TC10LAB.py and it is callable the same second -- no new .srv, no
gateway change, no client update. Raw SCPI is available too, because the
driver's own `command`/`query` are public methods (so they dispatch like any
other): method="query", args='["TEC:ACT?"]'.

Unlike galvo_node this one POLLS, at `publish_rate` (default 1 Hz): a
temperature loop is a sensor, clients need the trend, and the controller is idle
between commands anyway -- there is no multi-second upload to collide with.

CONNECTS AT STARTUP AND COMPLAINS LOUDLY IF IT CANNOT. A missing controller is
logged as an ERROR banner on every retry, not a one-line warning, because a
silent connected=false is how you discover at the end of an experiment that
nothing was ever temperature-controlled. The node still comes up (the rest of
the graph must not die with it) and reconnects by itself once you replug.

Meta-methods handled by the NODE, not the driver:
  list_methods()  -> [{name, signature, doc}] of everything callable
  reconnect()     -> re-open the session; True on success
  connected()     -> bool
"""

import math
import os
import threading

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from scopio_interfaces.msg import TemperatureStatus
from scopio_interfaces.srv import InstrumentCall

from .drivers import dispatch
from .drivers.TC10LAB import TC10LAB

META_METHODS = (
    {"name": "list_methods", "signature": "()",
     "doc": "List every method callable through this node."},
    {"name": "reconnect", "signature": "()",
     "doc": "Re-open the session to the controller; returns True on success."},
    {"name": "connected", "signature": "()",
     "doc": "Whether the node currently holds a live session."},
)


class TemperatureNode(Node):
    def __init__(self):
        super().__init__("temperature_node")
        self.declare_parameter("resource", "")
        self.declare_parameter("publish_rate", 1.0)
        self.declare_parameter("timeout_ms", 5000)
        self.declare_parameter("reconnect_period", 10.0)
        self.declare_parameter("units", "C")

        self._lock = threading.Lock()   # guards tc swaps; I/O is locked inside TC10LAB
        self.tc = None
        self.idn = ""
        self.last_error = ""
        self.state = {}                 # last good status() dict

        self._connect()

        self.status_pub = self.create_publisher(TemperatureStatus, "temperature/status", 5)
        cb = ReentrantCallbackGroup()
        self.create_service(InstrumentCall, "temperature/call", self._on_call,
                            callback_group=cb)

        rate = max(0.1, float(self.get_parameter("publish_rate").value))
        self.create_timer(1.0 / rate, self._publish_status, callback_group=cb)
        period = max(2.0, float(self.get_parameter("reconnect_period").value))
        self.create_timer(period, self._retry_connect, callback_group=cb)

    # ------------------------------------------------------------------ #
    #  Connection
    # ------------------------------------------------------------------ #
    def _connect(self):
        # TCLAB_RESOURCE wins over the param -- .env is the one place the rig is
        # described. The driver honours it too; resolving here just lets us log
        # what was chosen. Empty => the driver auto-discovers (char device first).
        resource = os.environ.get("TCLAB_RESOURCE") or self.get_parameter("resource").value
        tc = TC10LAB(resource, timeout_ms=int(self.get_parameter("timeout_ms").value))
        try:
            tc._open()                       # TC10LAB() does NOT open on construction
            idn = tc.idn()
            if "TC10" not in idn.upper() and "WAVELENGTH" not in idn.upper():
                raise RuntimeError(f"{tc.resource} answered '{idn}' -- that is not a "
                                   "TC LAB. Set TCLAB_RESOURCE to the right address.")
            units = str(self.get_parameter("units").value).strip()
            if units:
                tc.set_units(units)          # published degrees are never ambiguous
            with self._lock:
                self.tc = tc
                self.idn = idn
                self.last_error = ""
            self.get_logger().info(f"TC10 LAB connected on {tc.resource}: {idn}")
            return True
        except Exception as exc:
            # Close whatever _open() managed to grab -- otherwise a wrong-device
            # or bad-reply failure leaks an fd on every retry, forever.
            try:
                tc._close()
            except Exception:
                pass
            self.last_error = str(exc)
            self._scream(exc)
            return False

    def _scream(self, exc):
        """A missing temperature controller is not a footnote."""
        log = self.get_logger()
        log.error("=" * 68)
        log.error("TC10 LAB NOT CONNECTED -- nothing is temperature controlled.")
        log.error(f"  {exc}")
        log.error("  Check: instrument powered on and USB plugged in;")
        log.error("         ls -l /dev/usbtmc*  (needs crw-rw-rw-, see ros2_ws/udev/);")
        log.error("         TCLAB_RESOURCE in ros2_ws/.env if two USB instruments share the bus.")
        log.error("  Replug the instrument -- this node retries by itself.")
        log.error("=" * 68)

    def _retry_connect(self):
        if self.tc is None:
            self._connect()

    def _release(self):
        """Give up the session so the retry timer can rebuild it. Closes the raw
        handles WITHOUT sending SCPI (the link may already be dead, and a write
        would just burn a full timeout)."""
        with self._lock:
            tc, self.tc = self.tc, None
        if tc is None:
            return False
        try:
            tc._close()
        except Exception:
            pass
        return True

    def _fail(self, exc, response):
        """A BROKEN LINK costs the session (the retry timer rebuilds it); a bad
        command/argument does not -- one client's mistake must not knock the
        controller offline for everybody else."""
        self.last_error = str(exc)
        if dispatch.is_link_error(exc) and self._release():
            self.get_logger().error(f"TC10 LAB link lost ({exc}); will reconnect.")
        response.success = False
        response.error = f"{type(exc).__name__}: {exc}"
        return response

    # ------------------------------------------------------------------ #
    #  Status
    # ------------------------------------------------------------------ #
    def _publish_status(self):
        tc = self.tc
        if tc is not None:
            try:
                self.state = tc.status()
                self.last_error = ""
            except Exception as exc:
                self.state = {}
                self.last_error = str(exc)
                if dispatch.is_link_error(exc) and self._release():
                    self.get_logger().error(f"TC10 LAB link lost ({exc}); will reconnect.")

        s = self.state
        msg = TemperatureStatus()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.connected = self.tc is not None
        msg.idn = self.idn
        msg.temperature = float(s.get("temperature", math.nan))
        msg.setpoint = float(s.get("setpoint", math.nan))
        msg.current = float(s.get("current", math.nan))
        msg.voltage = float(s.get("voltage", math.nan))
        msg.output = bool(s.get("output", False))
        msg.in_tolerance = bool(s.get("in_tolerance", False))
        msg.units = str(s.get("units", ""))
        msg.condition = int(s.get("condition", 0))
        msg.faults = list(s.get("faults", []))
        msg.last_error = self.last_error
        self.status_pub.publish(msg)

    # ------------------------------------------------------------------ #
    #  The API: call any public method on the driver
    # ------------------------------------------------------------------ #
    def _on_call(self, request, response):
        method = (request.method or "").strip()
        response.result = "null"

        if method == "list_methods":
            response.success = True
            response.result = dispatch.to_json(dispatch.describe(TC10LAB, META_METHODS))
            response.error = ""
            return response
        if method == "connected":
            response.success = True
            response.result = dispatch.to_json(self.tc is not None)
            response.error = ""
            return response
        if method == "reconnect":
            self._release()
            ok = self._connect()
            response.success = ok
            response.result = dispatch.to_json(ok)
            response.error = "" if ok else (self.last_error or "reconnect failed")
            return response

        tc = self.tc
        if tc is None:
            response.success = False
            response.error = (f"TC10 LAB unavailable ({self.last_error or 'not connected'})")
            return response

        try:
            response.result = dispatch.call(tc, method, request.args, request.kwargs)
            self.last_error = ""
            response.success = True
            response.error = ""
        except dispatch.DispatchError as exc:      # bad request: link is fine
            response.success = False
            response.error = str(exc)
        except Exception as exc:                   # instrument or method fault
            self._fail(exc, response)
        return response

    def destroy_node(self):
        # Hand the front panel back, best-effort. The TEC output is deliberately
        # NOT switched off: a sample being held at temperature should survive a
        # backend restart. Turn it off explicitly if you want it off.
        tc = self.tc
        if tc is not None:
            try:
                tc.local()
            except Exception:
                pass
        self._release()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = TemperatureNode()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
