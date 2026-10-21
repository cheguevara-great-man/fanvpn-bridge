"""Opt-in Muse PC push adapter; the legacy file exchange stays the default.

Chat history has ONE background reader per active turn. Tool requests/results
travel independently so a history RPC cannot delay every sequential PC command.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import threading
import time

from .muse_network import configure_network
from .muse_pc import LIVE_PROTOCOL, PCSessions, PCWorkspace
from .muse_push_channel import ChannelReserve, PushChannel, relay_origin
from .muse_service import ServiceError, gateway_factory, make_server
from .muse_tools import MailboxCompatibilityError


@dataclass
class Exchange:
    channel: object
    stop: threading.Event = field(default_factory=threading.Event)
    confirmed: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    terminal: dict | None = None
    error: Exception | None = None
    watcher: threading.Thread | None = None

    def check(self):
        self.channel.check()
        if self.error is not None:
            raise ServiceError("Muse status monitoring failed; no automatic replay", 502,
                               "muse_web_transport_error")

    def watch(self, gateway, state, timeout):
        def read():
            try:
                while not self.stop.is_set():
                    events = PCSessions._history(gateway, state, min(timeout, 15))
                    if state.submitted_text is None:
                        self.confirmed.set()
                        terminal = PCSessions._terminal(events, state.baseline)
                        text = (terminal.get("display_text") or terminal.get("content")) if terminal else None
                        if isinstance(text, str) and text.strip():
                            self.terminal = terminal
                            break
                    # This only schedules chat-status checks, NOT tool delivery.
                    self.stop.wait(1)
            except Exception as error:
                self.error = error
            finally:
                self.finished.set()
        self.watcher = threading.Thread(target=read, daemon=True)
        self.watcher.start()

    def close(self):
        self.stop.set()
        self.channel.close()


class PushWorkspace(PCWorkspace):
    def __init__(self, root, config, channel_factory=PushChannel, *, prewarm=True):
        super().__init__(root)
        relay_origin(config["url"])
        self.config, self.channel_factory = config, channel_factory
        self.exchanges = {}
        self.lock = threading.Lock()
        self.reserve = ChannelReserve(config, channel_factory) if prewarm else None

    def check_compatible(self, gateway):
        super().check_compatible(gateway)
        source = self.read_bytes(gateway, self.root + "/scripts/pc.py")
        if b'"/rooms/{run}/call"' not in source:
            raise MailboxCompatibilityError("An independent push helper must be installed")

    def prepare(self, gateway, tools, timeout):
        digest = self.catalog(gateway, tools)
        if self.reserve is not None:
            run, expires, channel = self.reserve.take()
            expires = min(expires, time.time() + timeout)
        else:
            run = secrets.token_hex(16)
            expires = time.time() + min(timeout, 2400)
            channel = self.channel_factory(self.config, run, expires)
        try:
            self.write(gateway, self.path(run, "manifest"), {
                "schema_version": 2, "protocol": LIVE_PROTOCOL, "run": run,
                "expires_at": expires, "catalog_sha256": digest, "relay": channel.cloud_config,
            })
            with self.lock:
                self.exchanges[run] = Exchange(channel)
            return run
        except Exception:
            channel.close()
            raise

    def exchange(self, run):
        with self.lock:
            exchange = self.exchanges.get(run)
        if exchange is None:
            raise ServiceError("Push exchange no longer exists; no replay", 409,
                               "muse_web_session_missing")
        return exchange

    def discard(self, run):
        with self.lock:
            exchange = self.exchanges.pop(run, None)
        if exchange is not None:
            exchange.close()

    def publish_result(self, gateway, state, result):
        self.exchange(state.run).channel.result(state.sequence, result)


class PushSessions(PCSessions):
    def __init__(self, factory, *, relay_config, channel_factory=PushChannel, prewarm=True, **kwargs):
        super().__init__(factory, **kwargs)
        self.workspace = PushWorkspace(self.workspace.root, relay_config, channel_factory, prewarm=prewarm)

    def _park_gateway(self, state, gateway):
        # Keep healthy native chat connections until eviction/shutdown. Task and
        # tool deadlines still apply; a five-minute timer must not kill approval.
        if self._closed:
            gateway.close()
        else:
            state.gateway = gateway

    def _close_gateway(self, state):
        self.workspace.discard(state.run)
        super()._close_gateway(state)

    def close(self):
        if self.workspace.reserve is not None:
            self.workspace.reserve.close()
        super().close()
        with self.workspace.lock:
            runs = list(self.workspace.exchanges)
        for run in runs:
            self.workspace.discard(run)

    def _check_result_delivery(self, gateway, state):
        exchange = self.workspace.exchange(state.run)
        exchange.check()
        if exchange.terminal is not None:
            state.valid = False
            raise ServiceError("Muse stopped waiting before result delivery; no replay", 409,
                               "muse_web_tool_result_invalid")

    def _wait_for_output(self, gateway, state, tools, deadline, progress):
        exchange = self.workspace.exchange(state.run)
        if exchange.watcher is None:
            exchange.watch(gateway, state, self.timeout)
        queued = None
        try:
            while time.monotonic() < deadline:
                progress()
                exchange.check()
                terminal = exchange.terminal
                if terminal is not None:
                    text = terminal.get("display_text") or terminal.get("content")
                    if not isinstance(text, str) or not text.strip():
                        raise ServiceError("Muse returned an empty final response", 502)
                    # The watcher has finished its last gateway read. No second reader.
                    exchange.finished.wait(1)
                    self.workspace.discard(state.run)
                    return self._remember(state, self._response(text))
                if queued is None:
                    # Queue waiting wakes immediately on a network event. It is
                    # not a sleep or a query to the remote filesystem.
                    queued = exchange.channel.next_call(min(0.1, max(0, deadline - time.monotonic())))
                elif not exchange.confirmed.is_set():
                    exchange.confirmed.wait(0.05)
                if queued is not None and exchange.confirmed.is_set():
                    exchange.check()
                    if exchange.terminal is not None:
                        continue
                    call, expires = self.workspace.validate_request(queued, state.run, state.sequence, tools)
                    response = self._response("", call)
                    state.pending = {"call_id": response["output"][0]["call_id"], "custom": call["freeform"],
                                     "tools": tools, "expires": time.monotonic() +
                                     min(self.result_timeout, max(0, expires - time.time()))}
                    progress()
                    return self._remember(state, response)
            raise ServiceError("Muse push request timed out; no replay", 504, "muse_web_timeout")
        except BaseException:
            self.workspace.discard(state.run)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--relay-config", required=True, type=Path)
    parser.add_argument("--tool-workspace", required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    relay = json.loads(args.relay_config.read_text(encoding="utf-8-sig"))
    configure_network(args.proxy if args.proxy is not None else config.get("proxy", "auto"))
    sessions = PushSessions(gateway_factory(config), relay_config=relay, tool_workspace=args.tool_workspace)
    server = make_server(config, sessions)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sessions.close()


if __name__ == "__main__":
    main()
