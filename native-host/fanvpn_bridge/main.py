"""Native Messaging process entry point for the v2 bridge."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

from .config import load_config
from .codex_login import CodexLoginError, run_codex_login
from .dispatcher import NativeDispatcher
from .errors import BridgeError
from .framing import FramedMessageChannel
from .forward_proxy import ForwardProxyError, run_forward_proxy
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
    product_server = None
    try:
        product_server = create_http_server(
            config,
            routes,
            dispatcher,
            dispatcher,
            listen_port=8000,
            product_api_alias=True,
        )
    except OSError as error:
        log.warning("vscode_product_api_unavailable listen=%s:8000 error=%s", config.listen_host, error)
    if product_server is not None:
        threading.Thread(
            target=product_server.serve_forever,
            name="fanvpn-vscode-product-api",
            daemon=True,
        ).start()
        log.info("vscode_product_api_ready listen=%s:8000", config.listen_host)
    log.info("ready pid=%s listen=%s:%s", os.getpid(), config.listen_host, config.listen_port)
    dispatcher.wait_closed()
    log.info("native_channel_closed pid=%s", os.getpid())
    server.shutdown()
    server.server_close()
    if product_server is not None:
        product_server.shutdown()
        product_server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_runtime_logging()
    log = logging.getLogger("fanvpn_bridge.main")
    parser = argparse.ArgumentParser(description="FanVPN Bridge v2 native host")
    parser.add_argument("--config", type=Path, default=_default_config_path())
    parser.add_argument("--codex-login", action="store_true")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:18888")
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--login-timeout", type=float, default=600)
    parser.add_argument("--forward-proxy", action="store_true")
    parser.add_argument("--proxy-config", type=Path)
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=18889)
    args, _chrome_args = parser.parse_known_args(argv)
    try:
        if args.forward_proxy:
            if args.proxy_config is None:
                raise ForwardProxyError("--proxy-config is required with --forward-proxy")
            return run_forward_proxy(
                args.proxy_config,
                args.proxy_host,
                args.proxy_port,
            )
        if args.codex_login:
            result = run_codex_login(
                codex_home=args.codex_home,
                bridge_url=args.bridge_url,
                browser_path=args.browser,
                timeout_seconds=args.login_timeout,
            )
            print(f"Codex login saved to: {result.auth_path}", flush=True)
            if result.backup_path is not None:
                print(f"Previous credentials backed up to: {result.backup_path}", flush=True)
            return 0
        return run(args.config)
    except CodexLoginError as error:
        log.error("codex_login_error type=%s", type(error).__name__)
        print(f"CODEX_LOGIN_ERROR: {error}", file=sys.stderr, flush=True)
        return 2
    except ForwardProxyError as error:
        log.error("forward_proxy_error type=%s", type(error).__name__)
        print(f"FORWARD_PROXY_ERROR: {error}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 130
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
