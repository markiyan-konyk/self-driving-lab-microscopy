"""The gateway's foot in the ROS 2 graph.

One rclpy node ("gateway") spins on a MultiThreadedExecutor in a background
thread; FastAPI/uvicorn owns the main thread's asyncio loop. Everything that
crosses the boundary goes through loop.call_soon_threadsafe -- request
handlers NEVER spin rclpy and rclpy callbacks NEVER touch asyncio directly.

Responsibilities:
  * name resolution   -- "stage/jog" -> "/scopio/stage/jog" (absolute paths
                         starting with "/" pass through untouched)
  * type discovery    -- cached from the live graph, refreshed on miss, so a
                         future node (e.g. temperature) is reachable through
                         the generic endpoints with ZERO gateway changes
  * service calls     -- async, per-(name,type) client cache
  * subscriptions     -- QoS mirrored from the publisher (so latched topics
                         like `calibration` deliver their retained value)
  * telemetry cache   -- always-on subscriptions to the small SCOPIO state
                         topics, feeding GET /api/v1/status. DISCOVERED, not
                         listed: any `<anything>/status` or `<anything>/state`
                         topic under /scopio is picked up as it appears, so a
                         new node's status reaches every client with no gateway
                         change (see TELEMETRY_SUFFIXES)
"""

import threading
import time

import rclpy
from rclpy.action.graph import get_action_names_and_types
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from rosidl_runtime_py.utilities import get_message, get_service

from .conversion import BULKY_TYPES, build_msg, msg_to_jsonable, normalize_msg_type

NAMESPACE = "/scopio"

# Always present in GET /api/v1/status (relative to /scopio), null until their
# node has published -- clients key on these names, so they never go missing.
TELEMETRY_TOPICS = [
    "stage/position",
    "camera/state",
    "awg/status",
    "temperature/status",
    "relay/state",
    "calibration",
]

# THE CONVENTION FOR NEW NODES: publish your state on `<name>/status` (or
# `<name>/state`) and it appears in /status by itself. The graph is rescanned
# every DISCOVERY_PERIOD_S, so a node that starts after the gateway is found
# too. Video topics are never cached (see BULKY_TYPES).
TELEMETRY_SUFFIXES = ("status", "state")
DISCOVERY_PERIOD_S = 5.0


def resolve(path):
    """'stage/jog' -> '/scopio/stage/jog'; '/other/thing' passes through."""
    path = path.strip()
    if path.startswith("/"):
        return path
    return f"{NAMESPACE}/{path}"


class UnknownInterface(Exception):
    pass


class RosBridge:
    def __init__(self):
        self.node = None
        self.executor = None
        self.thread = None
        self.loop = None  # asyncio loop, set from the FastAPI startup hook
        self._lock = threading.Lock()
        self._service_types = {}
        self._topic_types = {}
        self._action_types = {}
        self._clients = {}
        self._telemetry = {}  # full topic name -> {"msg": jsonable, "stamp": float}
        self._telemetry_subs = {}  # full topic name -> rclpy subscription
        self._telemetry_lock = threading.Lock()
        self._started = time.time()

    # ---------------------------------------------------------- lifecycle
    def start(self):
        rclpy.init()
        self.node = Node("gateway", namespace=NAMESPACE)
        self.executor = MultiThreadedExecutor(num_threads=4)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True,
                                       name="rclpy-executor")
        self.thread.start()
        self._discover_telemetry()
        self.node.create_timer(DISCOVERY_PERIOD_S, self._discover_telemetry)

    def shutdown(self):
        try:
            self.executor.shutdown(timeout_sec=2.0)
            self.node.destroy_node()
            rclpy.shutdown()
        except Exception:
            pass

    @property
    def ok(self):
        return self.node is not None and rclpy.ok()

    @property
    def uptime_s(self):
        return time.time() - self._started

    # ------------------------------------------------------ type discovery
    def refresh_types(self):
        if self.node is None:
            # The gateway serves even when rclpy failed to start (see main.py),
            # so every graph lookup funnels through here for one clear reason.
            raise RuntimeError("gateway is not attached to a ROS graph "
                               "(see /api/v1/health: ros_ok)")
        with self._lock:
            self._service_types = {
                name: types[0]
                for name, types in self.node.get_service_names_and_types()
                if types
            }
            self._topic_types = {
                name: types[0]
                for name, types in self.node.get_topic_names_and_types()
                if types
            }
            self._action_types = {
                name: types[0]
                for name, types in get_action_names_and_types(self.node)
                if types
            }

    def _lookup(self, table_name, full_name):
        table = getattr(self, table_name)
        if full_name not in table:
            self.refresh_types()
            table = getattr(self, table_name)
        if full_name not in table:
            raise UnknownInterface(full_name)
        return table[full_name]

    def service_type(self, full_name):
        return self._lookup("_service_types", full_name)

    def topic_type(self, full_name):
        return self._lookup("_topic_types", full_name)

    def action_type(self, full_name):
        return self._lookup("_action_types", full_name)

    def tables(self):
        """Snapshot of all discovered interfaces (for /api/v1/interfaces)."""
        self.refresh_types()
        with self._lock:
            return (dict(self._service_types), dict(self._topic_types),
                    dict(self._action_types))

    # ------------------------------------------------------- asyncio bridge
    async def await_ros_future(self, fut, timeout):
        """Await an rclpy Future from the asyncio loop, with timeout."""
        import asyncio

        aio = self.loop.create_future()

        def _done(f):
            def _transfer():
                if aio.done():
                    return
                exc = f.exception()
                if exc is not None:
                    aio.set_exception(exc)
                else:
                    aio.set_result(f.result())
            self.loop.call_soon_threadsafe(_transfer)

        fut.add_done_callback(_done)
        return await asyncio.wait_for(aio, timeout)

    # ------------------------------------------------------- service calls
    def _client_for(self, full_name, type_str):
        key = (full_name, type_str)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                srv_cls = get_service(type_str)
                client = self.node.create_client(srv_cls, full_name)
                self._clients[key] = client
        return client

    async def call_service(self, path, body, timeout=10.0):
        """Generic JSON service call. Returns the response as a jsonable dict.

        Raises UnknownInterface (404), ValueError (bad fields, 422),
        asyncio.TimeoutError (504).
        """
        full_name = resolve(path)
        type_str = self.service_type(full_name)
        srv_cls = get_service(type_str)
        # nan_for_missing: on a service, an omitted float means "leave it alone".
        request = build_msg(srv_cls.Request, body or {}, nan_for_missing=True)
        client = self._client_for(full_name, type_str)
        fut = client.call_async(request)
        try:
            response = await self.await_ros_future(fut, timeout)
        except Exception:
            client.remove_pending_request(fut)
            raise
        return msg_to_jsonable(response)

    # ------------------------------------------------------- subscriptions
    def qos_for_topic(self, full_name, depth=10):
        """Mirror the live publisher's QoS so latched topics latch and
        best-effort topics don't force reliability."""
        infos = self.node.get_publishers_info_by_topic(full_name)
        durability = DurabilityPolicy.VOLATILE
        reliability = ReliabilityPolicy.RELIABLE
        if infos:
            if any(i.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL
                   for i in infos):
                durability = DurabilityPolicy.TRANSIENT_LOCAL
            if all(i.qos_profile.reliability == ReliabilityPolicy.BEST_EFFORT
                   for i in infos):
                reliability = ReliabilityPolicy.BEST_EFFORT
        return QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=depth,
            durability=durability,
            reliability=reliability,
        )

    def create_subscription(self, full_name, callback):
        """Subscribe to any topic; callback(jsonable_msg) runs in the executor
        thread. Returns the rclpy subscription (destroy it when done)."""
        type_str = self.topic_type(full_name)
        msg_cls = get_message(normalize_msg_type(type_str))

        def _cb(msg):
            callback(msg_to_jsonable(msg))

        return self.node.create_subscription(
            msg_cls, full_name, _cb, self.qos_for_topic(full_name))

    def create_publisher(self, full_name, type_str):
        msg_cls = get_message(normalize_msg_type(type_str))
        return self.node.create_publisher(msg_cls, full_name, 10), msg_cls

    # ---------------------------------------------------------- telemetry
    @staticmethod
    def is_telemetry(full_name):
        """Is this topic part of the /status snapshot? The fixed list, plus the
        naming convention every new node can follow (TELEMETRY_SUFFIXES)."""
        prefix = NAMESPACE + "/"
        if not full_name.startswith(prefix):
            return False
        rel = full_name[len(prefix):]
        return rel in TELEMETRY_TOPICS or rel.rsplit("/", 1)[-1] in TELEMETRY_SUFFIXES

    def _discover_telemetry(self):
        """Subscribe to every telemetry topic that has a publisher and is not
        cached yet. Runs at start and then on a timer, so nodes that come up
        after the gateway -- or are added later -- are picked up.

        Only once a PUBLISHER exists: the subscription mirrors its QoS
        (qos_for_topic), which is what makes latched topics (calibration,
        relay/state) deliver their retained value to this late joiner. A
        subscription made before the publisher existed would have to guess
        the durability, and a wrong guess on a publish-on-change topic receives
        nothing, ever."""
        try:
            topics = self.node.get_topic_names_and_types()
        except Exception:
            return
        for name, types in topics:
            if not types or not self.is_telemetry(name):
                continue
            with self._telemetry_lock:
                if name in self._telemetry_subs:
                    continue
            type_str = normalize_msg_type(types[0])
            if type_str in BULKY_TYPES:
                continue
            try:
                if not self.node.get_publishers_info_by_topic(name):
                    continue
                msg_cls = get_message(type_str)
                sub = self.node.create_subscription(
                    msg_cls, name, self._telemetry_cb(name),
                    self.qos_for_topic(name, depth=1))
            except Exception:
                continue           # an unimportable type: skip, retry next scan
            with self._telemetry_lock:
                self._telemetry_subs[name] = sub

    def _telemetry_cb(self, full_name):
        def _cb(msg):
            self._telemetry[full_name] = {"msg": msg_to_jsonable(msg),
                                          "stamp": time.time()}
        return _cb

    def telemetry_snapshot(self):
        """The fixed names first (null until published), then every
        discovered topic, by name relative to /scopio."""
        out = {rel: self._telemetry.get(resolve(rel)) for rel in TELEMETRY_TOPICS}
        prefix = NAMESPACE + "/"
        for full in sorted(list(self._telemetry)):
            rel = full[len(prefix):] if full.startswith(prefix) else full
            out.setdefault(rel, self._telemetry[full])
        return out


bridge = RosBridge()
