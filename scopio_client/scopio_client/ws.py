"""One background WebSocket connection multiplexing all subscriptions and
action goals for a Scopio client.

The gateway's envelope protocol (see docs/API.md): every frame is a JSON
object with an "op" and (usually) an "id" correlating request and responses.
This manager:

  * lazily connects on first use;
  * runs a single recv thread dispatching frames by id;
  * reconnects for as long as the client lives (1 s between attempts) and
    re-subscribes live subscriptions -- in-flight actions on a dropped
    connection fail with ScopioError;
  * is thread-safe for sends.
"""

import itertools
import json
import queue
import threading
import time

import websocket

from .errors import ScopioError


class Subscription:
    def __init__(self, manager, sub_id, topic, callback, rate_hz):
        self._manager = manager
        self.id = sub_id
        self.topic = topic
        self.callback = callback
        self.rate_hz = rate_hz

    def unsubscribe(self):
        self._manager.unsubscribe(self)


class WsManager:
    def __init__(self, ws_url, connect_timeout=10.0):
        self.ws_url = ws_url
        self.connect_timeout = connect_timeout
        self._ws = None
        self._lock = threading.Lock()          # guards connect + send
        self._ids = itertools.count(1)
        self._subs = {}                        # id -> Subscription
        self._waiters = {}                     # id -> Queue of envelopes
        self._thread = None
        self._closed = False

    # ------------------------------------------------------------ plumbing
    def _connect_locked(self):
        ws = websocket.create_connection(self.ws_url, timeout=self.connect_timeout)
        ws.settimeout(None)  # recv blocks; sends use the socket directly
        self._ws = ws
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._recv_loop, daemon=True,
                                            name="scopio-ws")
            self._thread.start()
        # Re-establish any subscriptions that survived a reconnect.
        for sub in list(self._subs.values()):
            ws.send(json.dumps({"op": "subscribe", "id": sub.id,
                                "topic": sub.topic, "rate_hz": sub.rate_hz}))

    def _send(self, envelope):
        with self._lock:
            if self._closed:
                raise ScopioError("client is closed")
            if self._ws is None:
                self._connect_locked()
            self._ws.send(json.dumps(envelope))

    def _recv_loop(self):
        while not self._closed:
            ws = self._ws
            if ws is None:
                # Keep retrying: the microscope may be rebooting, and
                # _connect_locked re-sends every surviving subscription.
                time.sleep(1.0)
                try:
                    with self._lock:
                        if not self._closed and self._ws is None:
                            self._connect_locked()
                except Exception:
                    pass
                continue
            try:
                raw = ws.recv()
                if not raw:
                    raise websocket.WebSocketConnectionClosedException()
                env = json.loads(raw)
            except Exception:
                if self._closed:
                    return
                self._ws = None
                # Fail everyone waiting on this connection (list(): the waiting
                # threads pop themselves out of the dict as they give up).
                for q in list(self._waiters.values()):
                    q.put({"op": "error", "code": "disconnected",
                           "detail": "WebSocket connection lost"})
                continue
            self._dispatch(env)

    def _dispatch(self, env):
        env_id = env.get("id")
        if env.get("op") == "message":
            sub = self._subs.get(env_id)
            if sub:
                try:
                    sub.callback(env.get("msg"), env)
                except Exception:
                    pass  # user callback errors must not kill the recv loop
            return
        waiter = self._waiters.get(env_id)
        if waiter is not None:
            waiter.put(env)

    def _wait(self, env_id, timeout, expect_ops):
        q = self._waiters[env_id]
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScopioError(f"timed out waiting for {expect_ops}")
            try:
                env = q.get(timeout=remaining)
            except queue.Empty:
                raise ScopioError(f"timed out waiting for {expect_ops}")
            if env.get("op") == "error":
                raise ScopioError(env.get("detail") or env.get("code"),
                                  payload=env)
            if env.get("op") in expect_ops:
                return env
            # anything else for this id (e.g. stale feedback) -- keep waiting

    # ------------------------------------------------------------- public
    def subscribe(self, topic, callback, rate_hz=None, timeout=10.0):
        sub_id = f"s{next(self._ids)}"
        sub = Subscription(self, sub_id, topic, callback, rate_hz)
        self._subs[sub_id] = sub
        self._waiters[sub_id] = queue.Queue()
        try:
            self._send({"op": "subscribe", "id": sub_id, "topic": topic,
                        "rate_hz": rate_hz})
            self._wait(sub_id, timeout, ("ok",))
        except Exception:
            self._subs.pop(sub_id, None)
            raise
        finally:
            self._waiters.pop(sub_id, None)
        return sub

    def unsubscribe(self, sub):
        self._subs.pop(sub.id, None)
        try:
            self._send({"op": "unsubscribe", "id": sub.id})
        except ScopioError:
            pass  # connection already gone -- server cleans up on close

    def send_goal(self, action, goal, on_feedback=None, timeout=600.0):
        """Send an action goal and BLOCK until its result. Returns
        {"status": "succeeded|aborted|canceled", "result": {...}}."""
        goal_id = f"g{next(self._ids)}"
        q = queue.Queue()
        self._waiters[goal_id] = q
        try:
            self._send({"op": "action_send_goal", "id": goal_id,
                        "action": action, "goal": goal or {}})
            ack = self._wait(goal_id, 15.0, ("action_ack",))
            if not ack.get("accepted", False):
                raise ScopioError(f"goal for '{action}' was rejected", payload=ack)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScopioError(f"action '{action}' timed out after {timeout}s")
                try:
                    env = q.get(timeout=remaining)
                except queue.Empty:
                    raise ScopioError(f"action '{action}' timed out after {timeout}s")
                op = env.get("op")
                if op == "action_feedback":
                    if on_feedback:
                        try:
                            on_feedback(env.get("feedback"))
                        except Exception:
                            pass
                elif op == "action_result":
                    return {"status": env.get("status"),
                            "result": env.get("result")}
                elif op == "error":
                    raise ScopioError(env.get("detail") or env.get("code"),
                                      payload=env)
        finally:
            self._waiters.pop(goal_id, None)

    def close(self):
        self._closed = True
        with self._lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
