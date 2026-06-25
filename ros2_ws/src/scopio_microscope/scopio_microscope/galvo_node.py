"""galvo_node - owns the optical-tweezer galvo laser (Rigol DG1022Z).

Reuses the validated driver and geometry from the repo (microscope/galvo.py and
microscope/tweezer.py) rather than reimplementing them. Publishes the laser
state (commanded volts + computed image/global position) and accepts pointing
commands, zeroing, and prolonged waveform goals.

Topics / services / actions (under /scopio):
  pub     laser/state        scopio_interfaces/LaserState
  sub     stage/position     scopio_interfaces/StagePosition  (for global pos)
  srv     tweezers/zero      scopio_interfaces/ZeroTweezers
  srv     laser/set          scopio_interfaces/SetLaser
  action  galvo/run_waveform scopio_interfaces/RunGalvoWaveform

Degrades gracefully: with no AWG (resource empty / connect fails) it publishes
connected=false and commands return success=false.
"""

import os
import threading
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from scopio_interfaces.msg import LaserState, StagePosition
from scopio_interfaces.srv import SetLaser, ZeroTweezers
from scopio_interfaces.action import RunGalvoWaveform

from . import repo_paths

repo_paths.ensure_on_path()
try:
    import galvo as galvo_driver      # microscope/galvo.py
    import tweezer                    # microscope/tweezer.py
except Exception as _imp_err:         # pragma: no cover - reported at runtime
    galvo_driver = None
    tweezer = None
    _IMPORT_ERROR = _imp_err
else:
    _IMPORT_ERROR = None


class GalvoNode(Node):
    def __init__(self):
        super().__init__("galvo_node")
        self.declare_parameter("resource", "")
        self.declare_parameter("publish_rate", 10.0)
        self.declare_parameter("jog_volts", 0.05)

        self.galvo = None
        self.stage_pos = {"x": 0, "y": 0, "z": 0}

        if tweezer is not None:
            tweezer.set_jog_volts(self.get_parameter("jog_volts").value)
        self._connect_galvo()

        self.state_pub = self.create_publisher(LaserState, "laser/state", 5)
        self.create_subscription(StagePosition, "stage/position", self._on_stage, 5)

        cb = ReentrantCallbackGroup()
        self.create_service(ZeroTweezers, "tweezers/zero", self._on_zero, callback_group=cb)
        self.create_service(SetLaser, "laser/set", self._on_set_laser, callback_group=cb)
        self._wave_server = ActionServer(
            self, RunGalvoWaveform, "galvo/run_waveform",
            execute_callback=self._execute_waveform,
            goal_callback=lambda g: GoalResponse.ACCEPT,
            cancel_callback=lambda c: CancelResponse.ACCEPT,
            callback_group=cb)

        rate = max(0.5, self.get_parameter("publish_rate").value)
        self.create_timer(1.0 / rate, self._publish_state)

    # ------------------------------------------------------------------ #
    def _connect_galvo(self):
        if galvo_driver is None:
            self.get_logger().warning(
                f"galvo/tweezer modules unavailable ({_IMPORT_ERROR}); laser disabled.")
            return
        resource = os.environ.get("GALVO_RESOURCE") or self.get_parameter("resource").value
        if not resource:
            self.get_logger().warning("No galvo resource set; laser disabled.")
            return
        try:
            self.galvo = galvo_driver.Galvo(resource)
            self.get_logger().info(f"Galvo connected: {self.galvo.idn}")
        except Exception as e:
            self.get_logger().warning(f"Galvo connect failed ({e}); laser disabled.")
            self.galvo = None

    def _on_stage(self, msg):
        self.stage_pos = {"x": msg.x, "y": msg.y, "z": msg.z}

    def _publish_state(self):
        msg = LaserState()
        msg.header.stamp = self.get_clock().now().to_msg()
        if self.galvo is None or tweezer is None:
            msg.connected = False
            self.state_pub.publish(msg)
            return
        st = tweezer.state(self.stage_pos, self.galvo)
        msg.connected = True
        msg.vx = float(st["vx"]); msg.vy = float(st["vy"])
        msg.home_vx = float(st["home"]["x"]); msg.home_vy = float(st["home"]["y"])
        msg.image_x = float(st["image_px"]["x"]); msg.image_y = float(st["image_px"]["y"])
        msg.global_x = float(st["global_um"]["x"])
        msg.global_y = float(st["global_um"]["y"])
        msg.global_z = float(st["global_um"]["z"])
        self.state_pub.publish(msg)

    # ------------------------------------------------------------------ #
    #  Services
    # ------------------------------------------------------------------ #
    def _on_zero(self, request, response):
        if self.galvo is None:
            response.success = False
            return response
        with tweezer.galvo_move_lock:
            tweezer.set_home(self.galvo.vx, self.galvo.vy)
        response.success = True
        response.home_vx = float(tweezer.home_volts["x"])
        response.home_vy = float(tweezer.home_volts["y"])
        return response

    def _on_set_laser(self, request, response):
        if self.galvo is None:
            response.success = False
            response.message = "Galvo unavailable"
            return response
        with tweezer.galvo_move_lock:
            if request.relative:
                vx, vy = self.galvo.nudge(request.vx, request.vy)
            else:
                vx, vy = self.galvo.set_volts(request.vx, request.vy)
        response.success = True
        response.vx = float(vx); response.vy = float(vy)
        response.message = "ok"
        return response

    # ------------------------------------------------------------------ #
    #  Action: prolonged waveform (one long command to the AWG)
    # ------------------------------------------------------------------ #
    def _program_waveform(self, req):
        """Issue the SCPI program for the requested shape. The AWG then runs it
        autonomously."""
        g = self.galvo
        shape = (req.shape or "sine").lower()
        amp = req.amplitude_vpp
        with tweezer.galvo_move_lock:
            if shape == "dc":
                g.point_mode()
            elif shape == "circle":
                f = req.x_freq_hz or 2.0
                g.apply_sine(1, f, amp, 0.0, phase_deg=req.x_phase_deg)
                g.apply_sine(2, f, amp, 0.0, phase_deg=req.y_phase_deg or 90.0)
                g.sync_phase()
            elif shape == "ramp":
                g.apply_ramp(1, req.x_freq_hz, amp, 0.0)
                g.apply_sine(2, req.y_freq_hz, amp, 0.0, phase_deg=req.y_phase_deg)
            else:  # sine / lissajous: independent sines per axis
                g.apply_sine(1, req.x_freq_hz, amp, 0.0, phase_deg=req.x_phase_deg)
                g.apply_sine(2, req.y_freq_hz, amp, 0.0, phase_deg=req.y_phase_deg)

    def _execute_waveform(self, goal_handle):
        req = goal_handle.request
        result = RunGalvoWaveform.Result()
        if self.galvo is None:
            goal_handle.abort()
            result.success = False
            result.message = "Galvo unavailable"
            return result
        try:
            self._program_waveform(req)
        except Exception as e:
            goal_handle.abort()
            result.success = False
            result.message = f"program failed: {e}"
            return result

        t0 = time.time()
        try:
            while True:
                elapsed = time.time() - t0
                if goal_handle.is_cancel_requested:
                    self._return_to_home()
                    goal_handle.canceled()
                    result.success = False
                    result.message = "canceled"
                    return result
                if req.duration_s > 0 and elapsed >= req.duration_s:
                    break
                fb = RunGalvoWaveform.Feedback()
                fb.elapsed_s = float(elapsed)
                goal_handle.publish_feedback(fb)
                time.sleep(0.2)
        finally:
            self._return_to_home()

        goal_handle.succeed()
        result.success = True
        result.message = "done"
        return result

    def _return_to_home(self):
        with tweezer.galvo_move_lock:
            try:
                self.galvo.point_mode()
                self.galvo.set_volts(tweezer.home_volts["x"], tweezer.home_volts["y"])
            except Exception as e:
                self.get_logger().warning(f"return-to-home failed: {e}")

    def destroy_node(self):
        try:
            if self.galvo is not None:
                self._return_to_home()
                self.galvo.off()
                self.galvo.close()
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
