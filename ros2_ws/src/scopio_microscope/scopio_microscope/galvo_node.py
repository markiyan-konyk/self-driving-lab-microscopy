"""galvo_node - a raw VISA/SCPI passthrough to the galvo/laser AWG.

DELIBERATELY INSTRUMENT-AGNOSTIC AND FROZEN. This node owns the VISA resource
(e.g. a Rigol DG1022Z) and does exactly one thing: relay command strings from
clients to the instrument and relay query responses back. It does NOT know what
the strings mean -- no volts, no pixels, no waveform types.

Why a string passthrough (see DECISIONS.md):
  * If the AWG is ever swapped for a different instrument, the ROS code does not
    change -- only the client's command strings do. The node stays "sacred".
  * One string can express DC, sine, square, or arbitrary waveforms and output
    on/off, without a combinatorial typed API. The laser GEOMETRY (volts->pixels
    ->micrometres, homing, jogging) lives in CLIENT code
    (ui/galvo_geometry.py), not here.

Topics / services (under /scopio):
  pub  awg/status   scopio_interfaces/AwgStatus   (connected, idn, last cmd/err)
  srv  awg/write    scopio_interfaces/AwgWrite    (send a SCPI command)
  srv  awg/query    scopio_interfaces/AwgQuery    (send a SCPI query, get reply)

Degrades gracefully: with no resource / no pyvisa it reports connected=false and
write/query return success=false; the rest of the graph still comes up.
"""

import os
import threading

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from scopio_interfaces.msg import AwgStatus
from scopio_interfaces.srv import AwgWrite, AwgQuery


class GalvoNode(Node):
    def __init__(self):
        super().__init__("galvo_node")
        self.declare_parameter("resource", "")
        self.declare_parameter("publish_rate", 5.0)
        self.declare_parameter("timeout_ms", 5000)

        self._lock = threading.Lock()       # serialize VISA access
        self.awg = None
        self.idn = ""
        self.last_command = ""
        self.last_error = ""

        self._connect()

        self.status_pub = self.create_publisher(AwgStatus, "awg/status", 5)
        cb = ReentrantCallbackGroup()
        self.create_service(AwgWrite, "awg/write", self._on_write, callback_group=cb)
        self.create_service(AwgQuery, "awg/query", self._on_query, callback_group=cb)

        rate = max(0.5, float(self.get_parameter("publish_rate").value))
        self.create_timer(1.0 / rate, self._publish_status, callback_group=cb)

    # ------------------------------------------------------------------ #
    def _connect(self):
        resource = os.environ.get("GALVO_RESOURCE") or self.get_parameter("resource").value
        try:
            import pyvisa
            rm = pyvisa.ResourceManager()
            if not resource:
                # Auto-discover the first USB instrument, matching how the proven
                # standalone tests find it (galvo_tests/_common.resolve_resource).
                # Without this the node silently stayed disabled unless the operator
                # remembered to export GALVO_RESOURCE.
                usb = [r for r in rm.list_resources() if r.upper().startswith("USB")]
                if usb:
                    resource = usb[0]
                    self.get_logger().info(f"Auto-selected AWG resource: {resource}")
            if not resource:
                self.get_logger().warning(
                    "No galvo VISA resource found (set GALVO_RESOURCE); AWG disabled.")
                return
            self.awg = rm.open_resource(resource)
            self.awg.timeout = int(self.get_parameter("timeout_ms").value)
            self.idn = self.awg.query("*IDN?").strip()
            self.get_logger().info(f"AWG connected: {self.idn}")
        except Exception as e:
            self.get_logger().warning(f"AWG connect failed ({e}); disabled.")
            self.awg = None
            self.last_error = str(e)

    def _publish_status(self):
        msg = AwgStatus()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.connected = self.awg is not None
        msg.idn = self.idn
        msg.last_command = self.last_command
        msg.last_error = self.last_error
        self.status_pub.publish(msg)

    # ------------------------------------------------------------------ #
    #  Services: raw passthrough
    # ------------------------------------------------------------------ #
    def _on_write(self, request, response):
        cmd = request.command
        if self.awg is None:
            response.success = False
            response.error = "AWG unavailable"
            return response
        try:
            with self._lock:
                self.awg.write(cmd)
            self.last_command = cmd
            self.last_error = ""
            response.success = True
            response.error = ""
        except Exception as e:
            self.last_error = str(e)
            response.success = False
            response.error = str(e)
        return response

    def _on_query(self, request, response):
        cmd = request.command
        if self.awg is None:
            response.success = False
            response.response = ""
            response.error = "AWG unavailable"
            return response
        try:
            with self._lock:
                reply = self.awg.query(cmd)
            self.last_command = cmd
            self.last_error = ""
            response.success = True
            response.response = reply.strip()
            response.error = ""
        except Exception as e:
            self.last_error = str(e)
            response.success = False
            response.response = ""
            response.error = str(e)
        return response

    def destroy_node(self):
        try:
            if self.awg is not None:
                self.awg.close()
        except Exception:
            pass
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
