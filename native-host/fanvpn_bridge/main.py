"""Native Messaging process entry point for the v2 bridge."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path

from .config import load_config
from .antigravity_setup import AntigravitySetupController
from .codex_login import CodexLoginError, run_codex_login
from .dispatcher import NativeDispatcher
from .device_config import DeviceConfigController
from .deepseek_harness import DeepSeekHarnessProvider
from .zen_models import ZenModelCatalog
from .zen_provider import ZenProvider
from .errors import BridgeError
from .framing import FramedMessageChannel
from .forward_proxy import ForwardProxyError, run_forward_proxy
from .gemini_account import GeminiAccountProvider
from .gemini_web import GeminiWebProvider
from .muse_web import MuseWebProvider
from .hybrid_route import HybridRouteStore
from .http_server import create_http_server
from .mode_control import CodexModeController
from .product_cache import ProductResponseCache
from .routing import RouteTable
from .runtime_logging import configure_runtime_logging
from .server_client import ServerClientError, default_server_client_config_path, run_server_client
from .server_executor_control import ServerExecutorTransportController
from .subagent_policy import SubagentPolicyStore
from .subagent_config import SubagentConfigurationController
from .usage_reporting import UsageReporter
from .update_control import LocalUpdateController


def run(config_path: Path) -> int:
    log = logging.getLogger("fanvpn_bridge.main")
    config = load_config(config_path)
    log.info("config_loaded pid=%s routes=%s", os.getpid(), ",".join(sorted(config.routes)))
    local_app_data = os.environ.get("LOCALAPPDATA")
    cache_base = (
        Path(local_app_data) / "FanVPNBridge"
        if local_app_data
        else Path.home() / ".fanvpn-bridge"
    )
    channel = FramedMessageChannel(sys.stdin.buffer, sys.stdout.buffer)
    subagent_policy = SubagentPolicyStore(cache_base / "subagent-policy.json")
    hybrid_route_store = HybridRouteStore(cache_base / "hybrid-route.json")
    gemini_account = GeminiAccountProvider(
        bridge_url=f"http://{config.listen_host}:{config.listen_port}",
        timeout_seconds=config.protocol.request_timeout_seconds,
    )
    gemini_web_url = os.environ.get("FANVPN_GEMINI_WEB_URL")
    try:
        # A local opt-in file also works when Chrome was started before the
        # user's shell environment changed. No account credentials belong here.
        gemini_web_config = cache_base / "gemini-web.json"
        if gemini_web_url is None and gemini_web_config.is_file():
            web_config = json.loads(gemini_web_config.read_text(encoding="utf-8"))
            if not isinstance(web_config, dict) or set(web_config) != {"base_url"} or not isinstance(web_config["base_url"], str):
                raise ValueError("Invalid Gemini Web configuration")
            gemini_web_url = web_config["base_url"]
        gemini_web = GeminiWebProvider(
            base_url=gemini_web_url,
            api_key=os.environ.get("FANVPN_GEMINI_WEB_API_KEY", ""),
            timeout_seconds=config.protocol.request_timeout_seconds,
        )
    except (OSError, ValueError):
        # An optional experimental provider must never stop the existing Bridge.
        log.warning("gemini_web_disabled reason=invalid_local_configuration")
        gemini_web = GeminiWebProvider()
    muse_web_url = os.environ.get("FANVPN_MUSE_WEB_URL")
    muse_web_key = os.environ.get("FANVPN_MUSE_WEB_API_KEY", "")
    try:
        muse_config_path = cache_base / "muse-web.json"
        if muse_web_url is None and muse_config_path.is_file():
            muse_config = json.loads(muse_config_path.read_text(encoding="utf-8"))
            if (not isinstance(muse_config, dict) or set(muse_config) != {"base_url", "api_key"}
                    or not all(isinstance(value, str) for value in muse_config.values())):
                raise ValueError("Invalid Muse Web local configuration")
            muse_web_url, muse_web_key = muse_config["base_url"], muse_config["api_key"]
        muse_web = MuseWebProvider(base_url=muse_web_url, api_key=muse_web_key,
                                   timeout_seconds=config.protocol.request_timeout_seconds)
    except (OSError, ValueError):
        log.warning("muse_web_disabled reason=invalid_local_configuration")
        muse_web = MuseWebProvider()
    server_executor_controller = ServerExecutorTransportController(cache_base=cache_base)
    if server_executor_controller.recover_selected_mode():
        log.info("server_executor_client_recovery_started port=18890")
    dispatcher = NativeDispatcher(
        channel,
        max_chunk_bytes=config.protocol.max_chunk_bytes,
        max_in_flight=config.protocol.max_in_flight,
        max_active_requests=config.protocol.max_active_requests,
        request_timeout_seconds=config.protocol.request_timeout_seconds,
        mode_controller=CodexModeController(),
        server_executor_controller=server_executor_controller,
        antigravity_setup_controller=AntigravitySetupController(),
        device_config_controller=DeviceConfigController(cache_base),
        subagent_config_controller=SubagentConfigurationController(
            Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")), subagent_policy
        ),
        gemini_account_provider=gemini_account,
        update_controller=LocalUpdateController(cache_base=cache_base),
    )
    muse_web.ticket_provider = dispatcher.get_muse_auth_ticket
    deepseek_harness = DeepSeekHarnessProvider(
        bridge_url=f"http://{config.listen_host}:{config.listen_port}",
        pow_solver=dispatcher.solve_deepseek_pow,
        timeout_seconds=config.protocol.request_timeout_seconds,
        state_path=cache_base / "deepseek-conversations.json",
    )
    # The Zen catalog is probed lazily on the first model-list request so a slow
    # or unreachable upstream never delays bridge startup.
    zen_provider = ZenProvider(
        catalog=ZenModelCatalog(cache_path=cache_base / "zen-models.json"),
        timeout_seconds=config.protocol.request_timeout_seconds,
    )
    dispatcher.start()
    routes = RouteTable(config.routes)
    product_cache = ProductResponseCache(
        persistent_directory=cache_base / "product-cache-v1"
    )
    usage_reporter = UsageReporter.load(cache_base, dispatcher)
    if usage_reporter is None:
        log.info("usage_reporting_disabled")
    else:
        log.info("usage_reporting_ready")
    server = create_http_server(
        config,
        routes,
        dispatcher,
        dispatcher,
        product_cache=product_cache,
        usage_reporter=usage_reporter,
        gemini_account=gemini_account,
        gemini_web=gemini_web,
        muse_web=muse_web,
        deepseek_harness=deepseek_harness,
        zen_provider=zen_provider,
        subagent_policy=subagent_policy,
        hybrid_route_store=hybrid_route_store,
    )
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
            product_cache=product_cache,
            usage_reporter=usage_reporter,
            gemini_account=gemini_account,
            gemini_web=gemini_web,
            muse_web=muse_web,
            deepseek_harness=deepseek_harness,
            zen_provider=zen_provider,
            subagent_policy=subagent_policy,
            hybrid_route_store=hybrid_route_store,
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
    if usage_reporter is not None:
        usage_reporter.close()
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
    parser.add_argument("--server-client", action="store_true")
    parser.add_argument("--server-client-config", type=Path, default=default_server_client_config_path())
    parser.add_argument("--server-client-host", default="127.0.0.1")
    parser.add_argument("--server-client-port", type=int, default=18890)
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
        if args.server_client:
            return run_server_client(
                args.server_client_config,
                host=args.server_client_host,
                port=args.server_client_port,
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
    except ServerClientError as error:
        log.error("server_client_error type=%s", type(error).__name__)
        print(f"SERVER_CLIENT_ERROR: {error}", file=sys.stderr, flush=True)
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
