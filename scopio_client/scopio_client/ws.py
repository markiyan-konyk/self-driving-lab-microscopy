"""One background WebSocket connection multiplexing all subscriptions, action
goals and publishes for a Scopio client.

The gateway's envelope protocol (see docs/API.md): every frame is a JSON
object with an "op" and (usually) an "id" correlating request and responses.
This manager:

  * lazily connects on first use;
  * runs a single recv thread dispatching frames by id;
  * KEEPS THE LINK HONEST: pings after PING_INTERVAL_S of silence and declares
    the connection dead after DEAD_AFTER_S with no frame at all. A socket whose
    far end vanished without closing (Pi rebooted, Wi-Fi roamed, NAT forgot
    it) otherwise blocks recv() forever -- every subscription silently freezes
    on its last value and nothing ever raises;
  * reconnects for as long as the client lives (1 s between attempts) and
    re-subscribes every CONFIRMED subscription;
  * fails whatever was in flight on a dropped connection with a ScopioError
    whose payload code is "disconnected" (a running goal keeps running on the
    microscope: the gateway ties it to the connection that sent it);
  * is thread-safe for sends.
"""

import itertools
import json
import queue
import threading
import time

import websocket

from .errors import ScopioError

PING_INTERVAL_S = 10.0   # recv timeout; a ping goes out after this much silence
DEAD_AFTER_S = 30.0      # no frame at all, not even a pong, for this long => dead

_DISCONNECTED = {"op": "error", "code": "disconnected",
                 "detail": "WebSocket connection lost"}


class Subscription:
    def __init__(self, manager, sub_id, topic, callback, rate_hz):
        self._manager = manager
        self.id = sub_id
        self.topic = topic
        self.callback = callback
        self.rate_hz = rate_hz
        # Only a subscription the gateway has confirmed is re-sent after a
        # reconnect; one still waiting for its "ok" fails instead, cleanly.
        self.active = False

    def unsubscribe(self):
        self._manager.unsubscribe(self)


class Goal:
    """A running action goal. `start_goal` returns one; `send_goal` is
    start + wait.

        goal = scope.start_goal("scan_region", {...})
        ...                                  # grab frames, read telemetry
        goal.feedback                        # latest feedback message
        goal.cancel()                        # from any thread
        goal.wait(timeout=600)               # {"status": ..., "result": ...}
    """

    def __init__(self, manager, goal_id, action, on_feedback=None):
        self._manager = manager
        self.id = goal_id
        self.action = action
        self.accepted = None
        self.status = None        # succeeded | aborted | canceled, once done
        self.result = None
        self.feedback = None      # the latest feedback message
        self.error = None         # the error envelope, if it failed
        self._on_feedback = on_feedback
        self._acked = threading.Event()
        self._done = threading.Event()

    def done(self):
        return self._done.is_set()

    def wait(self, timeout=None):
        """Block until the result; return {"status", "result"} or raise."""
        if not self._done.wait(timeout):
            raise ScopioError(f"action '{self.action}' timed out after {timeout}s")
        if self.error is not None:
            raise ScopioError(self.error.get("detail") or self.error.get("code"),
                              payload=self.error)
        return {"status": self.status, "result": self.result}

    def cancel(self):
        """Ask the microscope to cancel. Returns at once; wait() then reports
        status "canceled" (or the result, if it finished first)."""
        self._manager._cancel_quietly(self.id)

    # -- called from the recv thread
    def _deliver(self, env):
        op = env.get("op")
        if op == "action_ack":
            self.accepted = bool(env.get("accepted"))
            if not self.accepted:
                self.error = {**env, "detail": f"goal for '{self.action}' was rejected"}
                self._done.set()
            self._acked.set()
        elif op == "action_feedback":
            self.feedback = env.get("feedback")
            if self._on_feedback:
                try:
                    self._on_feedback(self.feedback)
                except Exception:
                    pass          # a user callback must not kill the recv loop
        elif op == "action_result":
            self.status, self.result = env.get("status"), env.get("result")
            self._done.set()
        elif op == "error":
            self.error = env
            self._acked.set()
            self._done.set()


class WsManager:
    def __init__(self, ws_url, connect_timeout=10.0):
        self.ws_url = ws_url
        self.connect_timeout = connect_timeout
        self._ws = None
        self._lock = threading.Lock()          # guards connect + send
        self._ids = itertools.count(1)
        self._subs = {}                        # id -> Subscription
        self._waiters = {}                     # id -> Queue of envelopes
        self._goals = {}                       # id -> Goal still running
        self._thread = None
        self._closed = False
        self._last_rx = 0.0

    # ------------------------------------------------------------ plumbing
    def _connect_locked(self):
        ws = websocket.create_connection(self.ws_url, timeout=self.connect_timeout)
        # A finite recv timeout is what lets _recv_loop notice silence at all.
        ws.settimeout(PING_INTERVAL_S)
        self._ws = ws
        self._last_rx = time.monotonic()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._recv_loop, daemon=True,
                                            name="scopio-ws")
            self._thread.start()
        # Re-establish the subscriptions the gateway had confirmed.
        for sub in list(self._subs.values()):
            if sub.active:
                ws.send(json.dumps({"op": "subscribe", "id": sub.id,
                                    "topic": sub.topic, "rate_hz": sub.rate_hz}))

    def _send(self, envelope):
        with self._lock:
            if self._closed:
                raise ScopioError("client is closed")
            if self._ws is None:
                try:
                    self._connect_locked()
                except Exception as exc:
                    where = self.ws_url.split("?")[0]      # never echo the key
                    raise ScopioError(f"cannot open the WebSocket to {where}: {exc}",
                                      payload=dict(_DISCONNECTED)) from exc
            self._ws.send(json.dumps(envelope))

    def _drop(self, ws):
        """The connection is gone: release it and fail everything waiting on it."""
        try:
            ws.close()        # else every reconnect leaks the dead socket
        except Exception:
            pass
        if self._ws is ws:
            self._ws = None
        # list(): the waiting threads pop themselves out as they give up.
        for q in list(self._waiters.values()):
            q.put(dict(_DISCONNECTED))
        for goal in list(self._goals.values()):
            goal._deliver(dict(_DISCONNECTED))
        self._goals.clear()

    def _recv_loop(self):
        while not self._closed:
            ws = self._ws
            if ws is None:
                # Keep retrying: the microscope may be rebooting, and
                # _connect_locked re-sends every confirmed subscription.
                time.sleep(1.0)
                try:
                    with self._lock:
                        if not self._closed and self._ws is None:
                            self._connect_locked()
                except Exception:
                    pass
                continue
            try:
                opcode, data = ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                if self._closed:
                    return
                if time.monotonic() - self._last_rx > DEAD_AFTER_S:
                    self._drop(ws)                 # silent far end: it is gone
                    continue
                try:
                    with self._lock:
                        ws.ping()
                except Exception:
                    self._drop(ws)
                continue
            except Exception:
                if self._closed:
                    return
                self._drop(ws)
                continue
            self._last_rx = time.monotonic()       # ANY frame proves it alive
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                self._drop(ws)
                continue
            if opcode not in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
                continue                           # ping/pong: liveness only
            try:
                env = json.loads(data)
            except ValueError:
                continue
            if isinstance(env, dict):
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
        goal = self._goals.get(env_id)
        if goal is not None:
            goal._deliver(env)
            if goal.done():
                self._goals.pop(env_id, None)
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
            # anything else for this id -- keep waiting

    def _request(self, envelope, timeout=10.0):
        """Send one envelope and wait for its "ok" (subscribe/publish/...)."""
        env_id = envelope["id"]
        self._waiters[env_id] = queue.Queue()
        try:
            self._send(envelope)
            return self._wait(env_id, timeout, ("ok",))
        finally:
            self._waiters.pop(env_id, None)

    # ------------------------------------------------------------- public
    def subscribe(self, topic, callback, rate_hz=None, timeout=10.0):
        sub_id = f"s{next(self._ids)}"
        sub = Subscription(self, sub_id, topic, callback, rate_hz)
        # Registered BEFORE the request goes out: a latched topic's retained
        # value arrives right behind the "ok", and must find its subscriber.
        self._subs[sub_id] = sub
        try:
            self._request({"op": "subscribe", "id": sub_id, "topic": topic,
                           "rate_hz": rate_hz}, timeout)
        except Exception:
            self._subs.pop(sub_id, None)
            raise
        sub.active = True
        return sub

    def unsubscribe(self, sub):
        self._subs.pop(sub.id, None)
        try:
            self._send({"op": "unsubscribe", "id": sub.id})
        except ScopioError:
            pass  # connection already gone -- server cleans up on close

    def publish(self, topic, msg, msg_type=None, timeout=10.0):
        """Publish one message on a topic (fire-and-forget on the ROS side)."""
        envelope = {"op": "publish", "id": f"p{next(self._ids)}",
                    "topic": topic, "msg": msg or {}}
        if msg_type:
            envelope["type"] = msg_type
        self._request(envelope, timeout)

    def start_goal(self, action, goal, on_feedback=None, ack_timeout=15.0):
        """Send an action goal; return a Goal as soon as it is ACCEPTED."""
        goal_id = f"g{next(self._ids)}"
        handle = Goal(self, goal_id, action, on_feedback)
        self._goals[goal_id] = handle
        try:
            self._send({"op": "action_send_goal", "id": goal_id,
                        "action": action, "goal": goal or {}})
        except Exception:
            self._goals.pop(goal_id, None)
            raise
        if not handle._acked.wait(ack_timeout):
            self._goals.pop(goal_id, None)
            raise ScopioError(f"goal for '{action}' was not acknowledged "
                              f"within {ack_timeout:.0f} s")
        if handle.error is not None:
            self._goals.pop(goal_id, None)
            raise ScopioError(handle.error.get("detail") or handle.error.get("code"),
                              payload=handle.error)
        return handle

    def send_goal(self, action, goal, on_feedback=None, timeout=600.0):
        """Send an action goal and BLOCK until its result. Returns
        {"status": "succeeded|aborted|canceled", "result": {...}}.

        Giving up CANCELS the goal: when the timeout runs out or the caller is
        interrupted (Ctrl-C), the goal is canceled on the microscope before this
        raises. A ROS goal otherwise outlives its caller, and a scan whose script
        died keeps moving the stage with nobody watching. (A dropped WebSocket
        cannot cancel: the gateway ties a goal to the connection that sent it.)"""
        handle = self.start_goal(action, goal, on_feedback)
        try:
            return handle.wait(timeout)
        except BaseException as exc:
            disconnected = (getattr(exc, "payload", None) or {}).get("code") == "disconnected"
            if disconnected or handle.done():
                raise
            handle.cancel()
            if isinstance(exc, ScopioError):
                raise ScopioError(f"action '{action}' timed out after {timeout}s "
                                  "(the goal was canceled)") from exc
            raise

    def _cancel_quietly(self, goal_id):
        try:
            self._send({"op": "action_cancel", "id": goal_id})
        except Exception:
            pass          # best effort: already on an error path

    def close(self):
        self._closed = True
        with self._lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
