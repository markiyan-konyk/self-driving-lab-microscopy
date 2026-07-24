"""galvo_node - the galvo/laser AWG, exposed whole.

The node owns the VISA session to the arbitrary-waveform generator (a Rigol
DG1022Z: CH1 = X mirror, CH2 = Y mirror) through the vendored, EDITABLE
`drivers/dg1022z.DG1022Z` class, and offers it to clients two ways:

  1. `awg/call`  -- ANY public method of the driver class, by name, with JSON
     args. Today that is `dcinit`, `dcupdate`, `dcmove`, `sininit`, `sinupdate`;
     the moment you add a method to dg1022z.py it is reachable here, with no new
     .srv, no gateway and no client change. Example: a UI button that nudges the
     laser calls `awg/call` with method="dcupdate", args=[1, 0.20].
  2. `awg/write` / `awg/query` -- raw SCPI passthrough, for clients that compose
     their own SCPI. The DG1022Z driver keeps its SCPI escape hatches commented
     out, so these route straight to the underlying VISA session it holds.

Connection is the NODE's job, not the client's: DG1022Z() does not open on
construction, so `_connect` builds the object and calls its `_open()`. `_open`
(and every other private, underscore-prefixed method) is therefore NOT callable
over `awg/call` -- re-opening the link is exposed as the `reconnect` meta-method
instead. The mirror geometry (volts<->pixels<->micrometres) stays in client
code; this node takes no view of what a command means.

Topics / services (under /scopio):
  pub  awg/status   scopio_interfaces/AwgStatus       (connected, idn, last cmd/err)
  srv  awg/call     scopio_interfaces/InstrumentCall  (any public driver method)
  srv  awg/write    scopio_interfaces/AwgWrite        (raw SCPI command)
  srv  awg/query    scopio_interfaces/AwgQuery        (raw SCPI query -> reply)

Meta-methods handled by the NODE, not the driver:
  list_methods()  -> [{name, signature, doc}] of everything callable (works
                     even while disconnected -- it introspects the class)
  reconnect()     -> re-open the VISA session (rebuild + _open); True on success
  connected()     -> bool

Degrades gracefully: with no resource / no instrument it reports connected=false
and calls fail with a readable error; the rest of the graph still comes up.
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
from .drivers.dg1022z import DG1022Z

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
        self.declare_parameter("init_on_connect", True)

        self._lock = threading.Lock()   # guards _gen swaps; VISA I/O is locked inside DG1022Z
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

        (The driver's own `_open` also honours a DAC_ID env var and will fall
        back to USB discovery itself, but we resolve here too so the node logs
        the choice and warns when several USB instruments share the bus.)
        Ethernet units can't be discovered (pyvisa-py doesn't scan the LAN) --
        name those explicitly.
        """
        resource = os.environ.get("GALVO_RESOURCE") or self.get_parameter("resource").value
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
            self.get_logger().warning(
                f"{len(usb)} USB instruments present {usb}; set GALVO_RESOURCE "
                "to pick the AWG deliberately.")
        self.get_logger().info(f"Auto-selected AWG resource: {usb[0]}")
        return usb[0]

    def _connect(self):
        resource = self._resolve_resource()
        try:
            # DG1022Z() does NOT open on construction -- the node opens it.
            gen = DG1022Z(resource, timeout_ms=int(self.get_parameter("timeout_ms").value))
            gen._open()
            idn = gen.device.query("*IDN?").strip()
            # Put both channels in DC mode at their offsets with outputs ON, so a
            # client's `update(ch, val)` positions the galvo immediately -- no
            # separate init call needed. Best-effort: a connected-but-uninitable
            # AWG still counts as connected. (Set init_on_connect:false to skip.)
            if self.get_parameter("init_on_connect").value:
                try:
                    gen.dcinit()
                except Exception as exc:
                    self.get_logger().warning(f"AWG dcinit failed ({exc}).")
            with self._lock:
                self.gen = gen
                self.idn = idn
                self.last_error = ""
            self.get_logger().info(f"AWG connected: {idn}")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.get_logger().warning(
                f"AWG connect failed ({exc}); node runs, reports connected=false.")
            return False

    def _retry_connect(self):
        if self.gen is None:
            self._connect()

    def _release(self):
        """Give up the session so the retry timer can rebuild it. Closes the raw
        VISA handles WITHOUT sending SCPI (the link may already be dead, and a
        write would just burn a full timeout)."""
        with self._lock:
            gen, self.gen = self.gen, None
        if gen is None:
            return False
        try:
            if gen.device is not None:
                gen.device.close()
            if gen.rm is not None:
                gen.rm.close()
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
    #  The API: call any public method on the driver
    # ------------------------------------------------------------------ #
    def _on_call(self, request, response):
        method = (request.method or "").strip()
        response.result = "null"

        if method == "list_methods":
            response.success = True
            response.result = dispatch.to_json(dispatch.describe(DG1022Z, META_METHODS))
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
    #  Raw SCPI passthrough (driver's command()/query() are commented out,
    #  so these talk to the VISA session the driver holds, under its lock).
    # ------------------------------------------------------------------ #
    def _on_write(self, request, response):
        gen = self.gen
        if gen is None:
            response.success = False
            response.error = "AWG unavailable"
            return response
        try:
            with gen._lock:
                gen.device.write(request.command)
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
            with gen._lock:
                reply = gen.device.query(request.command)
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
        # Clean shutdown: use the driver's own close (outputs off), best-effort,
        # then drop the session. On a restart the retry timer re-opens it.
        gen = self.gen
        if gen is not None:
            try:
                gen._close()
            except Exception:
                pass
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
