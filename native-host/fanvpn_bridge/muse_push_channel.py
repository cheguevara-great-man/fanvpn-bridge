"""Per-run push transport; no tool execution and no implicit reconnect/replay."""
from __future__ import annotations

import http.client
from concurrent.futures import Future
import json
import queue
import secrets
import threading
import time
from urllib.parse import urlsplit

MAX_BYTES = 2 * 1024 * 1024


class ChannelReserve:
    """Prepare at most one unused channel while login/model work proceeds.

    No task is attached until take(). Unused credentials expire after five
    minutes. Assigned channels are never reconnected, pooled or replayed.
    """

    def __init__(self, config, factory, idle_seconds=300):
        self.config, self.factory, self.idle_seconds = config, factory, idle_seconds
        self.lock = threading.RLock()
        self.slot = None
        self.timer = None
        self.closed = False
        self.warm()

    def warm(self):
        with self.lock:
            if self.closed or self.slot is not None:
                return
            future = self.slot = Future()

        def prepare():
            channel = None
            try:
                run, expires = secrets.token_hex(16), time.time() + 2400
                channel = self.factory(self.config, run, expires)
                with self.lock:
                    if self.closed:
                        channel.close()
                    elif self.slot is future:
                        future.set_result((run, expires, channel))
                        self.timer = threading.Timer(self.idle_seconds, self._expire, args=(future,))
                        self.timer.daemon = True
                        self.timer.start()
                    if not future.done():
                        future.set_result((run, expires, channel))
            except Exception as error:
                if channel is not None:
                    channel.close()
                if not future.done():
                    future.set_exception(error)

        threading.Thread(target=prepare, daemon=True).start()

    def _expire(self, future):
        with self.lock:
            if self.slot is not future:
                return
            self.slot, self.timer = None, None
        if future.done() and future.exception() is None:
            future.result()[2].close()

    def take(self):
        with self.lock:
            if self.closed:
                raise ConnectionError("Push reserve is closed")
            self.warm()
            future, self.slot = self.slot, None
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
        # Claim before waiting: the preparer must not mistake a claimed channel
        # for an expired unused one. An unused failed reserve has never carried
        # a task: it may be replaced once, unlike an assigned/in-flight channel.
        channel = None
        try:
            run, expires, channel = future.result()
            with self.lock:
                if self.closed:
                    raise ConnectionError("Push reserve is closed")
            channel.check()
        except Exception:
            if channel is not None:
                channel.close()
            with self.lock:
                if self.closed:
                    raise ConnectionError("Push reserve is closed") from None
            run, expires = secrets.token_hex(16), time.time() + 2400
            channel = self.factory(self.config, run, expires)
            try:
                channel.check()
                with self.lock:
                    if self.closed:
                        raise ConnectionError("Push reserve is closed")
            except Exception:
                channel.close()
                raise
        self.warm()
        return run, expires, channel

    def close(self):
        with self.lock:
            self.closed = True
            future, self.slot = self.slot, None
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
        if future is not None and future.done() and future.exception() is None:
            future.result()[2].close()


def relay_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Relay must be a clean HTTPS origin")
    parsed.port  # Validate the port before connecting.
    return parsed


class PushChannel:
    def __init__(self, config, run, expires):
        from websockets.sync.client import connect

        self.run, self.closed = run, False
        self.events = queue.Queue(maxsize=8)
        self.send_lock = threading.Lock()
        self.error = None
        self.metrics = []
        self.request_id = None
        self.expected_ack = None
        self.acknowledged = threading.Event()
        origin = relay_origin(config["url"])
        connection = http.client.HTTPSConnection(origin.hostname, origin.port or 443, timeout=10)
        try:
            connection.request("POST", "/rooms", json.dumps({"run": run, "expires_at": expires}),
                               {"Authorization": "Bearer " + config["admin_token"],
                                "Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read(4097))
            if response.status != 200:
                raise ConnectionError("Relay room creation failed")
        finally:
            connection.close()
        self.cloud_config = {"url": config["url"].rstrip("/"), "token": value["cloud_token"]}
        # Only the dedicated relay goes direct. Muse/proxy/system settings stay unchanged.
        self.socket = connect("wss://" + origin.netloc + f"/rooms/{run}/pc",
                              additional_headers={"Authorization": "Bearer " + value["pc_token"]},
                              proxy=None, open_timeout=10, max_size=MAX_BYTES,
                              ping_interval=20, ping_timeout=20)
        if json.loads(self.socket.recv(timeout=10)) != {"type": "ready"}:
            self.socket.close()
            raise ConnectionError("Relay did not confirm the PC channel")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while not self.closed:
                value = json.loads(self.socket.recv())
                if value.get("type") == "call":
                    self.metrics.append({"cloud_sent_ms": value.get("cloud_sent_ms"),
                                         "relay_received_ms": value.get("relay_received_ms"),
                                         "pc_received_ms": time.time_ns() / 1_000_000})
                    self.events.put_nowait(value)
                elif value.get("type") == "result_accepted":
                    if type(value.get("sequence")) is not int or value["sequence"] != self.expected_ack:
                        raise ConnectionError("Unexpected result acknowledgement")
                    self.acknowledged.set()
                else:
                    raise ConnectionError("Relay exchange ended; do not replay")
        except Exception:
            if not self.closed:
                self.error = ConnectionError("Push connection ended; no automatic replay")
                self.acknowledged.set()

    def next_call(self, timeout):
        self.check()
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            return None
        self.check()
        self.request_id = value["request_id"]
        return value["request"]

    def check(self):
        if self.error is not None:
            raise self.error
        if self.closed:
            raise ConnectionError("Push channel is closed")

    def result(self, sequence, result):
        self.check()
        if self.request_id is None:
            raise ValueError("No pending pushed request")
        self.acknowledged.clear()
        self.expected_ack = sequence
        with self.send_lock:
            self.socket.send(json.dumps({"type": "result", "request_id": self.request_id,
                                         "sequence": sequence, "result": result}, ensure_ascii=False))
        if not self.acknowledged.wait(10):
            raise TimeoutError("Result publication is uncertain; no automatic replay")
        self.check()
        self.request_id = None

    def close(self):
        self.closed = True
        self.acknowledged.set()
        self.socket.close()
        if threading.current_thread() is not self.reader:
            self.reader.join(timeout=2)
