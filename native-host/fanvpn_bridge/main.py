"""Native Messaging process entry point for the v2 bridge."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

from .config import load_config
from .dispatcher import NativeDispatcher
from .errors import BridgeError
from .framing import FramedMessageChannel
from .http_server import create_http_server
from .routing import RouteTable
from .runtime_logging import configure_runtime_logging


def run(config_path: Path) -> int:
    log = logging.getLogger("fanvpn_bridge.main")
    config = load_config(config_path)
    log.info("config_loaded pid=%s routes=%s", os.getpid(), ",".join(sorted(config.routes)))
    channel = FramedMessageChannel(sys.stdin.buffer, sys.stdout.buffer)
    dispatcher = NativeDispatcher(
        channel,
        max_chunk_bytes=config.protocol.max_chunk_bytes,
        max_in_flight=config.protocol.max_in_flight,
        max_active_requests=config.protocol.max_active_requests,
        request_timeout_seconds=config.protocol.request_timeout_seconds,
    )
    dispatcher.start()
    routes = RouteTable(config.routes)
    server = create_http_server(config, routes, dispatcher, dispatcher)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="fanvpn-loopback-http",
        daemon=True,
    )
    server_thread.start()
    log.info("ready pid=%s listen=%s:%s", os.getpid(), config.listen_host, config.listen_port)
    dispatcher.wait_closed()
    log.info("native_channel_closed pid=%s", os.getpid())
    server.shutdown()
    server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_runtime_logging()
    log = logging.getLogger("fanvpn_bridge.main")
    parser = argparse.ArgumentParser(description="FanVPN Bridge v2 native host")
    parser.add_argument("--config", type=Path, default=_default_config_path())
    args, _chrome_args = parser.parse_known_args(argv)
    try:
        return run(args.config)
    except BridgeError as error:
        log.error("bridge_error code=%s retryable=%s", error.code, error.retryable)
        print(str(error), file=sys.stderr, flush=True)
        return 1
    except Exception as error:  # Native Messaging stdout must remain protocol-only.
        log.exception("internal_error type=%s", type(error).__name__)
        print(f"INTERNAL_ERROR: {error}", file=sys.stderr, flush=True)
        return 1


def _default_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "routes.json"
    return Path(__file__).resolve().parents[2] / "config" / "routes.example.json"


if __name__ == "__main__":
    raise SystemExit(main())
