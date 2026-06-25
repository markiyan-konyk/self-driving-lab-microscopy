"""stage_node - owns the Sangaboard XYZ stage.

Publishes the microscope's global (open-loop) position and executes long-horizon
motion goals. It re-implements the small amount of stage logic leanly for ROS
rather than importing the Flask app's controls.py (which carries app-global
state).

Topics / actions (under /scopio):
  pub     stage/position    scopio_interfaces/StagePosition
  sub     beads             scopio_interfaces/BeadArray   (latest count, for scans)
  action  stage/move_path   scopio_interfaces/MoveStagePath
  action  scan_region       scopio_interfaces/ScanRegion

Degrades gracefully: with no board present it publishes connected=false and
move goals abort cleanly.
"""

import threading
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from scopio_interfaces.msg import BeadArray, StagePosition
from scopio_interfaces.action import MoveStagePath, ScanRegion


class StageNode(Node):
    def __init__(self):
        super().__init__("stage_node")
        self.declare_parameter("publish_rate", 5.0)
        self.declare_parameter("step_x", 40)
        self.declare_parameter("step_y", 40)
        self.declare_parameter("step_z", 40)

        self._lock = threading.Lock()
        self.sb = None
        self.position = {"x": 0, "y": 0, "z": 0}   # open-loop, origin at startup
        self._latest_beads = 0

        self._open_board()

        self.pos_pub = self.create_publisher(StagePosition, "stage/position", 5)
        self.create_subscription(BeadArray, "beads", self._on_beads, 5)

        rate = max(0.5, self.get_parameter("publish_rate").value)
        self.create_timer(1.0 / rate, self._publish_position)

        cb = ReentrantCallbackGroup()
        self._move_server = ActionServer(
            self, MoveStagePath, "stage/move_path",
            execute_callback=self._execute_move_path,
            goal_callback=lambda g: GoalResponse.ACCEPT,
            cancel_callback=lambda c: CancelResponse.ACCEPT,
            callback_group=cb)
        self._scan_server = ActionServer(
            self, ScanRegion, "scan_region",
            execute_callback=self._execute_scan_region,
            goal_callback=lambda g: GoalResponse.ACCEPT,
            cancel_callback=lambda c: CancelResponse.ACCEPT,
            callback_group=cb)

    # ------------------------------------------------------------------ #
    def _open_board(self):
        try:
            from sangaboard import Sangaboard
            self.sb = Sangaboard()
            self.get_logger().info("Sangaboard connected.")
        except Exception as e:
            self.get_logger().warning(f"Sangaboard unavailable ({e}); idling.")
            self.sb = None

    def _on_beads(self, msg):
        self._latest_beads = msg.count

    def _publish_position(self):
        msg = StagePosition()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.connected = self.sb is not None
        msg.x = int(self.position["x"])
        msg.y = int(self.position["y"])
        msg.z = int(self.position["z"])
        self.pos_pub.publish(msg)

    def _move_rel(self, dx, dy, dz):
        """Move by a relative displacement (steps) and track absolute position."""
        if self.sb is None:
            raise RuntimeError("Sangaboard unavailable")
        with self._lock:
            self.sb.move_rel([int(dx), int(dy), int(dz)])
            self.position["x"] += int(dx)
            self.position["y"] += int(dy)
            self.position["z"] += int(dz)

    def _move_abs(self, x, y, z):
        self._move_rel(x - self.position["x"], y - self.position["y"],
                       z - self.position["z"])

    # ------------------------------------------------------------------ #
    #  Action: move along a path of absolute targets
    # ------------------------------------------------------------------ #
    def _execute_move_path(self, goal_handle):
        req = goal_handle.request
        result = MoveStagePath.Result()
        reached = 0
        for i, pt in enumerate(req.points):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                result.success = False
                result.points_reached = reached
                return result
            try:
                self._move_abs(pt.x, pt.y, pt.z)
            except Exception as e:
                self.get_logger().error(f"move_path failed at {i}: {e}")
                goal_handle.abort()
                result.success = False
                result.points_reached = reached
                return result
            reached += 1
            fb = MoveStagePath.Feedback()
            fb.current_index = i
            fb.x = self.position["x"]
            fb.y = self.position["y"]
            fb.z = self.position["z"]
            goal_handle.publish_feedback(fb)
            if req.settle_s > 0:
                time.sleep(req.settle_s)
        goal_handle.succeed()
        result.success = True
        result.points_reached = reached
        return result

    # ------------------------------------------------------------------ #
    #  Action: jump-scan a rectangular region (boustrophedon grid)
    # ------------------------------------------------------------------ #
    def _execute_scan_region(self, goal_handle):
        req = goal_handle.request
        result = ScanRegion.Result()
        step = max(1, req.step)
        xs = list(range(req.x_min, req.x_max + 1, step))
        ys = list(range(req.y_min, req.y_max + 1, step))
        visited = 0
        for j, y in enumerate(ys):
            row = xs if j % 2 == 0 else list(reversed(xs))   # snake to cut travel
            for x in row:
                if goal_handle.is_cancel_requested:
                    goal_handle.canceled()
                    result.success = False
                    result.frames_visited = visited
                    return result
                try:
                    self._move_abs(x, y, self.position["z"])
                except Exception as e:
                    self.get_logger().error(f"scan failed: {e}")
                    goal_handle.abort()
                    result.success = False
                    result.frames_visited = visited
                    return result
                if req.settle_s > 0:
                    time.sleep(req.settle_s)
                visited += 1
                fb = ScanRegion.Feedback()
                fb.frames_visited = visited
                fb.x = self.position["x"]
                fb.y = self.position["y"]
                fb.beads_in_frame = self._latest_beads
                goal_handle.publish_feedback(fb)
        goal_handle.succeed()
        result.success = True
        result.frames_visited = visited
        return result

    def destroy_node(self):
        try:
            if self.sb is not None and hasattr(self.sb, "close"):
                self.sb.close()
        except Exception:
            pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = StageNode()
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
