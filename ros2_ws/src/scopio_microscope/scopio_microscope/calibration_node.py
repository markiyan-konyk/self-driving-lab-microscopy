"""calibration_node - owns the microscope's spatial calibration.

The single source of truth for how pixels and steps map to micrometres:
  * um_per_px      - image scale (micrometres per native camera pixel)
  * steps_per_um_* - stage conversion (Sangaboard steps per micrometre per axis)

It publishes the calibration on a LATCHED (transient-local) topic so any client
-- including the stage_node, which needs steps_per_um to report its position in
micrometres -- gets the current value immediately on join, and it persists the
calibration to disk so it survives restarts (no recalibrating every launch).

Topics / services (under /scopio):
  pub  calibration       scopio_interfaces/Calibration   (latched)
  srv  calibration/set   scopio_interfaces/CalibrationSet
"""

import json
import math
import os

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, HistoryPolicy

from scopio_interfaces.msg import Calibration
from scopio_interfaces.srv import CalibrationSet


class CalibrationNode(Node):
    def __init__(self):
        super().__init__("calibration_node")
        self.declare_parameter("calibration_file", "calibration.json")
        self.path = self.get_parameter("calibration_file").value

        self.data = {
            "um_per_px": None,
            "steps_per_um": {"x": 1.0, "y": 1.0, "z": 1.0},
        }
        self._load()

        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             history=HistoryPolicy.KEEP_LAST)
        self.pub = self.create_publisher(Calibration, "calibration", latched)
        self.create_service(CalibrationSet, "calibration/set", self._on_set)
        self._publish()
        self.get_logger().info(f"Calibration loaded from {self.path}: {self.data}")

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if "um_per_px" in d:
                self.data["um_per_px"] = d["um_per_px"]
            spu = d.get("steps_per_um") or {}
            for ax in ("x", "y", "z"):
                if ax in spu:
                    self.data["steps_per_um"][ax] = float(spu[ax])
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except OSError as e:
            self.get_logger().warning(f"Could not persist calibration: {e}")

    def _publish(self):
        msg = Calibration()
        msg.header.stamp = self.get_clock().now().to_msg()
        upp = self.data["um_per_px"]
        msg.has_um_per_px = upp is not None
        msg.um_per_px = float(upp) if upp is not None else 0.0
        msg.steps_per_um_x = float(self.data["steps_per_um"]["x"])
        msg.steps_per_um_y = float(self.data["steps_per_um"]["y"])
        msg.steps_per_um_z = float(self.data["steps_per_um"]["z"])
        self.pub.publish(msg)

    def _on_set(self, request, response):
        if not math.isnan(request.um_per_px):
            if request.um_per_px <= 0:
                response.success = False
                response.message = "um_per_px must be > 0"
                return response
            self.data["um_per_px"] = float(request.um_per_px)
        for ax, val in (("x", request.steps_per_um_x), ("y", request.steps_per_um_y),
                        ("z", request.steps_per_um_z)):
            if not math.isnan(val) and val > 0:
                self.data["steps_per_um"][ax] = float(val)
        self._save()
        self._publish()
        response.success = True
        response.message = "ok"
        return response


def main(args=None):
    rclpy.init(args=args)
    node = CalibrationNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
