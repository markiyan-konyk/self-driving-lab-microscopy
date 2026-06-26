"""tracker_node - live trackpy bead tracking (on-demand).

Subscribes to the camera's compressed image and runs trackpy to report bead
positions + count + clump flag. Tracking is OFF by default and toggled with a
service, so the microscope only does this heavy work when an external computer
asks for it (the "service system, not always publishing" requirement).

trackpy is mandatory here (OpenCV was validated as unreliable for this
footage). For real-time the loop runs in two modes that share the same trackpy
parameters as the offline viscosity pipeline (viscosity/track.py DEFAULTS):
  * ACQUIRE: full-frame tp.locate (periodic / when nothing is tracked)
  * TRACK:   per-bead ROI tp.locate on small crops (cheap, real-time)
followed by nearest-neighbour linking with a small memory.

Topics / services (under /scopio):
  sub  image/compressed   sensor_msgs/CompressedImage
  pub  beads              scopio_interfaces/BeadArray
  srv  tracker/set_active std_srvs/SetBool
"""

import rclpy
from rclpy.node import Node

from std_srvs.srv import SetBool
from sensor_msgs.msg import CompressedImage
from scopio_interfaces.msg import Bead, BeadArray

from . import repo_paths

repo_paths.ensure_on_path()
try:
    import numpy as np
    import cv2
    import trackpy as tp
    tp.quiet()
    try:
        import track as viscosity_track    # viscosity/track.py (for DEFAULTS)
        _DEFAULTS = getattr(viscosity_track, "DEFAULTS", {})
    except Exception:
        _DEFAULTS = {}
    _DEPS_OK = True
except Exception as _imp_err:              # pragma: no cover
    _DEPS_OK = False
    _IMPORT_ERROR = _imp_err


class Track:
    __slots__ = ("id", "x", "y", "mass", "size", "ecc", "missing")

    def __init__(self, tid, x, y, mass, size, ecc):
        self.id = tid
        self.x = x; self.y = y
        self.mass = mass; self.size = size; self.ecc = ecc
        self.missing = 0


class TrackerNode(Node):
    def __init__(self):
        super().__init__("tracker_node")
        self.declare_parameter("active_on_start", False)
        self.declare_parameter("diameter", 11)
        self.declare_parameter("minmass", 800.0)
        self.declare_parameter("channel", 1)
        self.declare_parameter("invert", False)
        self.declare_parameter("percentile", 64.0)
        self.declare_parameter("search_range", 5.0)
        self.declare_parameter("memory", 3)
        self.declare_parameter("roi_size", 31)
        self.declare_parameter("acquire_every_n", 30)
        self.declare_parameter("clump_size_factor", 1.6)

        p = self.get_parameter
        self.diameter = int(p("diameter").value) | 1     # force odd
        self.minmass = float(p("minmass").value)
        self.channel = int(p("channel").value)
        self.invert = bool(p("invert").value)
        self.percentile = float(p("percentile").value)
        self.search_range = float(p("search_range").value)
        self.memory = int(p("memory").value)
        self.roi = int(p("roi_size").value)
        self.acquire_every_n = int(p("acquire_every_n").value)
        self.clump_factor = float(p("clump_size_factor").value)

        self.active = bool(p("active_on_start").value) and _DEPS_OK
        self.tracks = []
        self._next_id = 0
        self._frame_i = 0

        self.pub = self.create_publisher(BeadArray, "beads", 5)
        self.create_subscription(CompressedImage, "image/compressed", self._on_image, 5)
        self.create_service(SetBool, "tracker/set_active", self._on_set_active)

        if not _DEPS_OK:
            self.get_logger().warning(
                f"tracking deps unavailable ({_IMPORT_ERROR}); tracker idle.")
        else:
            self.get_logger().info(
                f"tracker ready (diameter={self.diameter}, minmass={self.minmass}); "
                f"active={self.active}")

    # ------------------------------------------------------------------ #
    def _on_set_active(self, request, response):
        if request.data and not _DEPS_OK:
            response.success = False
            response.message = "tracking dependencies unavailable"
            return response
        self.active = bool(request.data)
        if not self.active:
            self.tracks = []
        response.success = True
        response.message = f"tracking {'on' if self.active else 'off'}"
        self.get_logger().info(response.message)
        return response

    def _on_image(self, msg):
        if not self.active:
            return
        try:
            buf = np.frombuffer(bytes(msg.data), dtype=np.uint8)
            bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if bgr is None:
                return
            plane = bgr[:, :, self.channel]
            self._frame_i += 1
            full = (self._frame_i % max(1, self.acquire_every_n) == 0) or not self.tracks
            dets = self._acquire(plane) if full else self._track_roi(plane)
            self._link(dets)
            self._publish(msg.header)
        except Exception as e:
            self.get_logger().warning(f"tracking frame failed: {e}")

    # ----- detection -----
    def _locate(self, plane):
        f = tp.locate(plane, self.diameter, minmass=self.minmass,
                      invert=self.invert, percentile=self.percentile)
        return f

    def _acquire(self, plane):
        """Full-frame detection -> list of (x, y, mass, size, ecc)."""
        f = self._locate(plane)
        return [(r["x"], r["y"], r.get("mass", 0.0), r.get("size", 0.0),
                 r.get("ecc", 0.0)) for _, r in f.iterrows()]

    def _track_roi(self, plane):
        """Per-track ROI detection: locate within a small crop around each
        track's last position. Cheap enough for real-time on the Pi."""
        h, w = plane.shape[:2]
        half = self.roi // 2
        dets = []
        for t in self.tracks:
            x0 = max(0, int(t.x) - half); x1 = min(w, int(t.x) + half + 1)
            y0 = max(0, int(t.y) - half); y1 = min(h, int(t.y) + half + 1)
            crop = plane[y0:y1, x0:x1]
            if crop.shape[0] < self.diameter or crop.shape[1] < self.diameter:
                continue
            try:
                f = self._locate(crop)
            except Exception:
                continue
            if len(f) == 0:
                continue
            r = f.loc[f["mass"].idxmax()]      # strongest feature in the crop
            dets.append((r["x"] + x0, r["y"] + y0, r.get("mass", 0.0),
                         r.get("size", 0.0), r.get("ecc", 0.0)))
        return dets

    # ----- nearest-neighbour linking -----
    def _link(self, dets):
        sr = self.search_range
        unmatched = list(range(len(dets)))
        for t in self.tracks:
            best, best_d = None, sr
            for di in unmatched:
                d = ((dets[di][0] - t.x) ** 2 + (dets[di][1] - t.y) ** 2) ** 0.5
                if d <= best_d:
                    best, best_d = di, d
            if best is not None:
                x, y, mass, size, ecc = dets[best]
                t.x, t.y, t.mass, t.size, t.ecc, t.missing = x, y, mass, size, ecc, 0
                unmatched.remove(best)
            else:
                t.missing += 1
        # New tracks from leftover detections
        for di in unmatched:
            x, y, mass, size, ecc = dets[di]
            self.tracks.append(Track(self._next_id, x, y, mass, size, ecc))
            self._next_id += 1
        # Drop tracks that have been missing too long
        self.tracks = [t for t in self.tracks if t.missing <= self.memory]

    # ----- output -----
    def _publish(self, header):
        live = [t for t in self.tracks if t.missing == 0]
        msg = BeadArray()
        msg.header = header
        sizes = [t.size for t in live if t.size > 0]
        median = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
        clump_thresh = self.clump_factor * median if median else float("inf")
        clumps = 0
        for t in live:
            b = Bead()
            b.id = int(t.id)
            b.x = float(t.x); b.y = float(t.y)
            b.mass = float(t.mass); b.size = float(t.size); b.ecc = float(t.ecc)
            msg.beads.append(b)
            if t.size > clump_thresh:
                clumps += 1
        msg.count = len(live)
        msg.clump_count = clumps
        self.pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = TrackerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
