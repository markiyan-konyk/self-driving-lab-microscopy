"""galvo_node - the galvo/laser AWG, exposed whole.

The node owns the VISA session to the arbitrary-waveform generator (a Rigol
DG1022Z: CH1 = X mirror, CH2 = Y mirror) through the vendored
`drivers/wavegen.WaveGen` class, and offers it to clients two ways:

  1. `awg/call`  -- ANY public method of the driver class, by name, with JSON
     args. Sine, sweep, burst, modulation, arb upload, paced command bursts,
     error-queue drain: whatever the class can do, a client can do. New driver
     methods are reachable the moment they exist -- no new .srv, no gateway or
     client change.
  2. `awg/write` / `awg/query` -- the original raw SCPI passthrough, unchanged,
     because a raw string still is the right tool when the client composes its
     own SCPI (galvo_draw, ui/galvo_geometry.py). These now route through the
     driver's `command()` / `query()`, which is what the class calls them.

What the class buys us over the old bare-pyvisa passthrough: one lock so
concurrent clients can't interleave mid-protocol, auto-recovery (USBTMC clear,
then reconnect) after a link hiccup instead of a dead node, and `send_sequence`
pacing for the long command bursts that used to wedge the session.

The node still takes NO view of what the commands mean: no volts, no pixels, no
waveform semantics. Laser GEOMETRY (volts->pixels->micrometres, homing, jogging)
stays in client code (ui/galvo_geometry.py, galvo_draw/) -- see DECISIONS.md.

Topics / services (under /scopio):
  pub  awg/status   scopio_interfaces/AwgStatus       (connected, idn, last cmd/err)
  srv  awg/call     scopio_interfaces/InstrumentCall  (any driver method)
  srv  awg/write    scopio_interfaces/AwgWrite        (raw SCPI command)
  srv  awg/query    scopio_interfaces/AwgQuery        (raw SCPI query -> reply)

Meta-methods handled by the NODE, not the driver:
  list_methods()  -> [{name, signature, doc}] of everything callable (works
                     even while disconnected -- it introspects the class)
  reconnect()     -> re-open the VISA session; True on success
  connected()     -> bool

Degrades gracefully: with no resource / no pyvisa it reports connected=false and
calls return success=false; the rest of the graph still comes up.
"""

import os
import threading

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from scopio_interfaces.msg import AwgStatus
from scopio_interfaces.srv import AwgQuery, AwgWrite, InstrumentCall

from .drivers import dispatch
from .drivers.wavegen import WaveGen

META_METHODS = (
    {"name": "list_methods", "signature": "()",
     "doc": "List every method callable through this node."},
    {"name": "reconnect", "signature": "()",
     "doc": "Re-open the VISA session to the instrument; returns True on success."},
    {"name": "connected", "signature": "()",
     "doc": "Whether the node currently holds a live session."},
)


class GalvoNode(Node):
    def __init__(self):
        super().__init__("galvo_node")
        self.declare_parameter("resource", "")
        self.declare_parameter("publish_rate", 5.0)
        self.declare_parameter("timeout_ms", 15000)
        self.declare_parameter("reconnect_period", 15.0)
        self.declare_parameter("auto_discover", True)

        self._lock = threading.Lock()   # guards _gen swaps; VISA I/O is locked inside WaveGen
        self.gen = None
        self.idn = ""
        self.last_command = ""
        self.last_error = ""

        self._connect()

        self.status_pub = self.create_publisher(AwgStatus, "awg/status", 5)
        cb = ReentrantCallbackGroup()
        self.create_service(InstrumentCall, "awg/call", self._on_call, callback_group=cb)
        self.create_service(AwgWrite, "awg/write", self._on_write, callback_group=cb)
        self.create_service(AwgQuery, "awg/query", self._on_query, callback_group=cb)

        rate = max(0.5, float(self.get_parameter("publish_rate").value))
        self.create_timer(1.0 / rate, self._publish_status, callback_group=cb)
        period = max(2.0, float(self.get_parameter("reconnect_period").value))
        self.create_timer(period, self._retry_connect, callback_group=cb)

    # ------------------------------------------------------------------ #
    #  Connection
    # ------------------------------------------------------------------ #
    def _resolve_resource(self):
        """GALVO_RESOURCE env > `resource` param > the first USB instrument.

        Auto-discovery matters: without it the node silently stayed disabled
        unless the operator remembered to export GALVO_RESOURCE. Ethernet units
        can't be discovered (pyvisa-py doesn't scan the LAN) -- name those.
        """
        resource = os.environ.get("GALVO_RESOURCE") or self.get_parameter("resource").value
        if resource or not self.get_parameter("auto_discover").value:
            return resource
        try:
            import pyvisa
            # Same backend the driver opens with, so we list what it can open.
            usb = [r for r in pyvisa.ResourceManager("@py").list_resources()
                   if r.upper().startswith("USB")]
        except Exception as exc:
            self.get_logger().warning(f"VISA enumeration failed ({exc}).")
            return ""
        if not usb:
            return ""
        if len(usb) > 1:
            # The temperature controller is a USB instrument too.
            self.get_logger().warning(
                f"{len(usb)} USB instruments present {usb}; set GALVO_RESOURCE "
                "to pick the AWG deliberately.")
        self.get_logger().info(f"Auto-selected AWG resource: {usb[0]}")
        return usb[0]

    def _connect(self):
        resource = self._resolve_resource()
        if not resource:
            self.get_logger().warning(
                "No galvo VISA resource found (set GALVO_RESOURCE); AWG disabled.")
            return False
        try:
            gen = WaveGen(resource, timeout_ms=int(self.get_parameter("timeout_ms").value))
            idn = gen.idn()
            with self._lock:
                self.gen = gen
                self.idn = idn
                self.last_error = ""
            self.get_logger().info(f"AWG connected: {idn}")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.get_logger().warning(f"AWG connect failed ({exc}); disabled.")
            return False

    def _retry_connect(self):
        if self.gen is None:
            self._connect()

    def _release(self):
        """Give up the session so the retry timer can rebuild it."""
        with self._lock:
            gen, self.gen = self.gen, None
        if gen is None:
            return False
        try:
            gen.close()
        except Exception:
            pass
        return True

    def _fail(self, exc, response):
        """Record a failed call. A BROKEN LINK costs the session (the reconnect
        timer rebuilds it); a bad command/argument does not -- one client's
        mistake must not knock the AWG offline for everybody else."""
        self.last_error = str(exc)
        if dispatch.is_link_error(exc) and self._release():
            self.get_logger().warning(f"AWG link lost ({exc}); will reconnect.")
        response.success = False
        response.error = f"{type(exc).__name__}: {exc}"
        return response

    def _publish_status(self):
        # Cached state only -- deliberately no polling: a status query at 5 Hz
        # would share the USBTMC session with multi-second arbitrary-waveform
        # uploads. The driver reports link health through the calls themselves.
        msg = AwgStatus()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.connected = self.gen is not None
        msg.idn = self.idn
        msg.last_command = self.last_command
        msg.last_error = self.last_error
        self.status_pub.publish(msg)

    # ------------------------------------------------------------------ #
    #  The API: call any method on the driver
    # ------------------------------------------------------------------ #
    def _on_call(self, request, response):
        method = (request.method or "").strip()
        response.result = "null"

        if method == "list_methods":
            response.success = True
            response.result = dispatch.to_json(dispatch.describe(WaveGen, META_METHODS))
            response.error = ""
            return response
        if method == "connected":
            response.success = True
            response.result = dispatch.to_json(self.gen is not None)
            response.error = ""
            return response
        if method == "reconnect":
            self._release()
            ok = self._connect()
            response.success = ok
            response.result = dispatch.to_json(ok)
            response.error = "" if ok else (self.last_error or "reconnect failed")
            return response

        gen = self.gen
        if gen is None:
            response.success = False
            response.error = f"AWG unavailable ({self.last_error or 'not connected'})"
            return response

        try:
            response.result = dispatch.call(gen, method, request.args, request.kwargs)
            self.last_command = f"{method}()"
            self.last_error = ""
            response.success = True
            response.error = ""
        except dispatch.DispatchError as exc:      # bad request: link is fine
            response.success = False
            response.error = str(exc)
        except Exception as exc:                   # instrument or method fault
            self._fail(exc, response)
        return response

    # ------------------------------------------------------------------ #
    #  Raw SCPI passthrough (unchanged contract, now via the driver)
    # ------------------------------------------------------------------ #
    def _on_write(self, request, response):
        gen = self.gen
        if gen is None:
            response.success = False
            response.error = "AWG unavailable"
            return response
        try:
            gen.command(request.command)
            self.last_command = request.command
            self.last_error = ""
            response.success = True
            response.error = ""
        except Exception as exc:
            self._fail(exc, response)
        return response

    def _on_query(self, request, response):
        gen = self.gen
        if gen is None:
            response.success = False
            response.response = ""
            response.error = "AWG unavailable"
            return response
        try:
            reply = gen.query(request.command)
            self.last_command = request.command
            self.last_error = ""
            response.success = True
            response.response = (reply or "").strip()
            response.error = ""
        except Exception as exc:
            response.response = ""
            self._fail(exc, response)
        return response

    def destroy_node(self):
        # Leave the mirrors wherever the client parked them: a node restart
        # must not slam a galvo or drop an experiment's beam.
        self._release()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = GalvoNode()
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
