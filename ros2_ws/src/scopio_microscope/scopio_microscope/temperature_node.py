"""temperature_node - the sample-temperature controller, exposed whole.

This node owns the VISA session to a Wavelength Electronics TC LAB (USB/USBTMC
or Ethernet/VXI-11) through the vendored `drivers/tclab.TCLab` class, and it
publishes THE ENTIRE CLASS as one generic service. Every method the driver has
-- setpoint, PID, IntelliTune, limits, sensor profiles, profiles, scripts, and
the raw `command`/`query` escape hatches -- is callable by any client, today.

Why this shape (and not a service per feature):
  * A self-driving lab cannot predict which knob the next experiment needs. A
    typed service per method would mean editing ROS, rebuilding the container
    and updating every client each time someone wants, say, a tolerance window.
  * So the ROS layer takes no view on which features are "supported": the
    driver class decides, each app picks its subset, and the node stays a thin,
    stable owner of the hardware. Same reasoning as the AWG (docs/DECISIONS.md).

Topics / services (under /scopio):
  pub  temperature/status   scopio_interfaces/TemperatureStatus  (polled state)
  srv  temperature/call     scopio_interfaces/InstrumentCall     (any method)

Meta-methods handled by the NODE, not the driver:
  list_methods()  -> [{name, signature, doc}] of everything callable (works
                     even while disconnected -- it introspects the class)
  reconnect()     -> re-open the VISA session; True on success
  connected()     -> bool

Degrades gracefully: with no resource / no pyvisa / the box switched off it
reports connected=false, retries in the background, and calls fail with a
readable error. The rest of the graph comes up regardless.
"""

import os
import threading

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from scopio_interfaces.msg import TemperatureStatus
from scopio_interfaces.srv import InstrumentCall

from .drivers import dispatch
from .drivers.tclab import TCLab

# Node meta-methods, advertised alongside the driver's own in list_methods.
META_METHODS = (
    {"name": "list_methods", "signature": "()",
     "doc": "List every method callable through this node."},
    {"name": "reconnect", "signature": "()",
     "doc": "Re-open the VISA session to the instrument; returns True on success."},
    {"name": "connected", "signature": "()",
     "doc": "Whether the node currently holds a live session."},
)


class TemperatureNode(Node):
    def __init__(self):
        super().__init__("temperature_node")
        self.declare_parameter("resource", "")
        self.declare_parameter("publish_rate", 1.0)
        self.declare_parameter("timeout_ms", 5000)
        self.declare_parameter("reconnect_period", 15.0)
        self.declare_parameter("units", TCLab.CELSIUS)
        self.declare_parameter("auto_discover", True)

        self._lock = threading.Lock()   # guards _tc swaps; VISA I/O is locked inside TCLab
        self.tc = None
        self.idn = ""
        self.last_method = ""
        self.last_error = ""
        self._state = {}                # newest good poll, published as-is

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
    def _resolve_resource(self):
        """TCLAB_RESOURCE env > `resource` param > first USB instrument.

        Ethernet units cannot be auto-discovered (pyvisa-py does not scan the
        LAN), so a TCPIP resource must always be named explicitly.
        """
        resource = os.environ.get("TCLAB_RESOURCE") or self.get_parameter("resource").value
        if resource or not self.get_parameter("auto_discover").value:
            return resource
        try:
            import pyvisa
            usb = [r for r in pyvisa.ResourceManager("@py").list_resources()
                   if r.upper().startswith("USB")]
        except Exception as exc:
            self.get_logger().warning(f"VISA enumeration failed ({exc}).")
            return ""
        if not usb:
            return ""
        if len(usb) > 1:
            # The AWG is a USB instrument too -- if both are on USB, name this
            # one explicitly instead of gambling on enumeration order.
            self.get_logger().warning(
                f"{len(usb)} USB instruments present {usb}; set TCLAB_RESOURCE "
                "to pick the temperature controller deliberately.")
        self.get_logger().info(f"Auto-selected temperature resource: {usb[0]}")
        return usb[0]

    def _connect(self):
        resource = self._resolve_resource()
        if not resource:
            self.get_logger().warning(
                "No temperature VISA resource (set TCLAB_RESOURCE); node runs "
                "but reports connected=false.")
            return False
        try:
            tc = TCLab(resource, timeout_ms=int(self.get_parameter("timeout_ms").value))
            tc.set_units(int(self.get_parameter("units").value))   # so published degrees mean something
            with self._lock:
                self.tc = tc
                self.idn = tc.idn
                self.last_error = ""
            self.get_logger().info(f"Temperature controller connected: {tc.idn}")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.get_logger().warning(f"Temperature connect failed ({exc}); retrying.")
            return False

    def _retry_connect(self):
        if self.tc is None:
            self._connect()

    def _release(self):
        """Give up the session so the retry timer can rebuild it. Returns
        whether there was one. Uses the driver's quiet close, NOT close(): that
        sends LOCAL, and a round-trip to an instrument we already believe is
        dead costs a full VISA timeout plus the driver's reconnect backoff."""
        with self._lock:
            tc, self.tc = self.tc, None
        if tc is None:
            return False
        try:
            tc._close_quiet()
        except Exception:
            pass
        return True

    def _drop(self, exc):
        """A BROKEN LINK costs the session (the reconnect timer rebuilds it); a
        bad argument or a method that raised on its own does not -- one client's
        mistake must not knock the controller offline for everybody else."""
        self.last_error = str(exc)
        if dispatch.is_link_error(exc) and self._release():
            self.get_logger().warning(f"Temperature link lost ({exc}); will reconnect.")

    # ------------------------------------------------------------------ #
    #  Status polling
    # ------------------------------------------------------------------ #
    def _poll(self, tc):
        """One pass of queries. TCLab serializes them against service calls."""
        cond = tc.condition()
        return {
            "temperature": tc.temperature(),
            "setpoint": tc.get_setpoint(),
            "aux_temperature": tc.aux_temperature(),
            "tec_current": tc.tec_current(),
            "tec_voltage": tc.tec_voltage(),
            "units": tc.get_units(),
            "output_enabled": tc.output_enabled(),
            "condition": cond,
            "in_tolerance": bool(cond & 512),
            "at_current_limit": bool(cond & 1),
            "sensor_fault": bool(cond & (64 | 32)),   # sensor open or shorted
        }

    def _publish_status(self):
        tc = self.tc                      # one read: the session can be dropped
        if tc is not None:                # by a service call at any moment
            try:
                self._state = self._poll(tc)
                self.last_error = ""
            except Exception as exc:
                # On a broken link this drops the session, so `connected` goes
                # false while the last good readings stay in the message --
                # clients can grey them out instead of seeing a fake 0.0.
                self._drop(exc)

        s = self._state
        msg = TemperatureStatus()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.connected = self.tc is not None
        msg.idn = self.idn
        msg.temperature = float(s.get("temperature", 0.0))
        msg.setpoint = float(s.get("setpoint", 0.0))
        msg.aux_temperature = float(s.get("aux_temperature", 0.0))
        msg.tec_current = float(s.get("tec_current", 0.0))
        msg.tec_voltage = float(s.get("tec_voltage", 0.0))
        msg.units = int(s.get("units", TCLab.CELSIUS))
        msg.output_enabled = bool(s.get("output_enabled", False))
        msg.in_tolerance = bool(s.get("in_tolerance", False))
        msg.at_current_limit = bool(s.get("at_current_limit", False))
        msg.sensor_fault = bool(s.get("sensor_fault", False))
        msg.condition = int(s.get("condition", 0))
        msg.last_method = self.last_method
        msg.last_error = self.last_error
        self.status_pub.publish(msg)

    # ------------------------------------------------------------------ #
    #  The API: call any method on the driver
    # ------------------------------------------------------------------ #
    def _on_call(self, request, response):
        method = (request.method or "").strip()
        response.result = "null"

        # --- node meta-methods (answerable without hardware) ---
        if method == "list_methods":
            response.success = True
            response.result = dispatch.to_json(dispatch.describe(TCLab, META_METHODS))
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
            response.error = ("temperature controller unavailable "
                              f"({self.last_error or 'not connected'})")
            return response

        try:
            response.result = dispatch.call(tc, method, request.args, request.kwargs)
            self.last_method = method
            self.last_error = ""
            response.success = True
            response.error = ""
        except dispatch.DispatchError as exc:       # bad request: link is fine
            response.success = False
            response.error = str(exc)
        except Exception as exc:                    # instrument fault or a
            self.last_method = method               # method that raised
            self._drop(exc)
            response.success = False
            response.error = f"{type(exc).__name__}: {exc}"
        return response

    def destroy_node(self):
        # Leave the box exactly as the operator left it -- do NOT silently
        # disable the TEC output here: an experiment may depend on it holding
        # temperature across a node restart. Just release the session.
        try:
            if self.tc is not None:
                self.tc.close()
        except Exception:
            pass
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
