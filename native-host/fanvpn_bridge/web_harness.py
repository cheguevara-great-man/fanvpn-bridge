"""Local WebHarness runtime boundary; never accepts a caller-supplied upstream."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import re
import tomllib
import base64
import select
import socket
import sys
import time
from pathlib import Path

WEB_PREFIX = "chatgpt-web/"
WEB_PORT = 17841
_HOP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "upgrade",
                "proxy-authorization", "proxy-authenticate", "authorization", "cookie", "set-cookie",
                "keep-alive", "te", "trailer", "chatgpt-account-id", "openai-organization", "openai-project"}


def runtime_home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BrowserAIBridge" / "WebHarness"


def is_web_model(value: object) -> bool:
    return isinstance(value, str) and value.startswith(WEB_PREFIX)


def runtime_token(home: Path | None = None) -> str:
    config = json.loads(((home or runtime_home()) / "config.json").read_text(encoding="utf-8"))
    token = config.get("controlToken")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,}", token):
        raise WebHarnessError("WebHarness control credential is unavailable")
    return token


def clean_web_history(payload: dict) -> dict:
    """Decode only upstream's transparent local checkpoints, never native blobs."""
    items = payload.get("input")
    if not isinstance(items, list) or not any(
        isinstance(item, dict) and (
            isinstance(item.get("encrypted_content"), str)
            and item["encrypted_content"].startswith(("ocx1:", "ocxr1:"))
            or item.get("type") == "reasoning" and item.get("encrypted_content") is None
            and isinstance(item.get("id"), str) and re.fullmatch(r"rs_[0-9a-fA-F]{32}", item["id"])
            and (isinstance(item.get("summary"), list) or isinstance(item.get("content"), list))
        ) for item in items
    ):
        return payload
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        row = dict(item)
        row.pop("id", None)
        encrypted = row.get("encrypted_content")
        if row.get("type") == "compaction" and isinstance(encrypted, str) and encrypted.startswith("ocx1:"):
            try:
                summary = base64.b64decode(encrypted[5:], validate=True).decode("utf-8")
            except (ValueError, UnicodeError) as error:
                raise WebHarnessError("Invalid WebHarness history checkpoint") from error
            cleaned.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text":
                "Context checkpoint from the previous model. Use it to continue the user's task:\n\n" + summary}]})
            continue
        if row.get("type") == "reasoning" and isinstance(encrypted, str) and encrypted.startswith("ocxr1:"):
            row.pop("encrypted_content", None)
            if not row.get("summary") and not row.get("content"):
                continue
        if row.get("type") == "reasoning" and row.get("encrypted_content") is None:
            row.pop("encrypted_content", None)
            if not row.get("summary") and not row.get("content"):
                continue
        cleaned.append(row)
    result = dict(payload, input=cleaned)
    result.pop("previous_response_id", None)
    return result


class WebHarnessError(RuntimeError):
    pass


class WebHarnessController:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or runtime_home()
        self._lock = threading.Lock()

    def _json(self, path: str, body: object = None, headers: dict | None = None,
              port: int = WEB_PORT) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body).encode() if body is not None else None,
                               {"Content-Type": "application/json", "X-Bridge-Web-Token": runtime_token(self.home), **(headers or {})})
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 4 * 1024 * 1024:
                raise WebHarnessError(f"WebHarness returned HTTP {response.status}")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise WebHarnessError("Invalid WebHarness response")
            return value
        finally:
            connection.close()

    def status(self) -> dict:
        result = {"installed": False, "ready": False, "running": False, "mode": "unconfigured"}
        try:
            install = json.loads((self.home / "installation.json").read_text(encoding="utf-8"))
            executable = (self.home / install["directory"] / "WebHarness.exe").resolve()
            executable.relative_to(self.home.resolve())
            result["installed"] = executable.is_file()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            health = self._json("/bridge/health")
            if health.get("service") != "codex-chatgpt-web":
                raise WebHarnessError("Unexpected listener on WebHarness port")
            result.update(running=True, ready=health.get("accepting_turns") is True,
                          mode=health.get("mode"), version=health.get("version"),
                          active_turns=health.get("active_browser_turns", 0))
            config = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
            result["interaction"] = config.get("browserInteractionMode", "automatic")
        except (OSError, ValueError, WebHarnessError, http.client.HTTPException):
            pass
        return result

    def open(self) -> dict:
        with self._lock:
            try:
                install = json.loads((self.home / "installation.json").read_text(encoding="utf-8"))
                executable = (self.home / install["directory"] / "WebHarness.exe").resolve()
                executable.relative_to(self.home.resolve())
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise WebHarnessError("请先安装网页执行器") from error
            if not executable.is_file():
                raise WebHarnessError("网页执行器文件不存在，请重新安装")
            network_path = self.home / "network.json"
            if not network_path.exists():
                source = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FanVPNBridge" / "direct-proxy.json"
                if source.is_file():
                    self._write_network("gateway")
            network = self._read_network()
            if network.get("tunnelProxyUrl") == "http://127.0.0.1:18889":
                self._ensure_gateway_proxy()
            environment = dict(os.environ)
            environment.update(BRIDGE_WEB_MANAGED="1", CODEX_CHATGPT_WEB_HOME=str(self.home),
                               CODEX_WEB_GPT_LAUNCHER_DATA_DIR=str(self.home / "browser"))
            subprocess.Popen([str(executable)], env=environment, cwd=executable.parent,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.status()

    def _read_network(self) -> dict:
        try:
            value = json.loads((self.home / "network.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"mode": "system"}
        except (OSError, ValueError):
            return {"mode": "system"}

    def _write_network(self, mode: str) -> None:
        value = {"mode": mode}
        source = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FanVPNBridge" / "direct-proxy.json"
        if source.is_file():
            from .forward_proxy import load_upstream_config
            load_upstream_config(source)
            value["tunnelProxyUrl"] = "http://127.0.0.1:18889"
            if mode == "gateway":
                value["browserProxyUrl"] = "http://127.0.0.1:18889"
        elif mode == "gateway":
            raise WebHarnessError("尚未保存服务器直连配置")
        self.home.mkdir(parents=True, exist_ok=True)
        destination = self.home / "network.json"
        temporary = destination.with_suffix(".next")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, destination)

    @staticmethod
    def _gateway_proxy_ready() -> bool:
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection("127.0.0.1", 18889, timeout=0.5)
            connection.request(
                "GET",
                "http://browser-ai-bridge.local/ready",
                headers={"Host": "browser-ai-bridge.local"},
            )
            response = connection.getresponse()
            if response.status != 200:
                return False
            payload = json.loads(response.read())
            return isinstance(payload, dict) and payload.get("mode") == "vscode-direct-proxy"
        except (OSError, ValueError, http.client.HTTPException):
            return False
        finally:
            if connection is not None:
                connection.close()

    def _ensure_gateway_proxy(self) -> None:
        if self._gateway_proxy_ready():
            return
        source = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FanVPNBridge" / "direct-proxy.json"
        from .forward_proxy import load_upstream_config
        load_upstream_config(source)
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [sys.executable, "-m", "fanvpn_bridge.main"]
        command.extend(["--forward-proxy", "--proxy-config", str(source), "--proxy-port", "18889"])
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        pid_path = source.parent / "direct-proxy.pid"
        pid_temporary = pid_path.with_name(f"{pid_path.name}.{os.getpid()}.next")
        pid_temporary.write_text(str(process.pid), encoding="ascii")
        os.replace(pid_temporary, pid_path)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._gateway_proxy_ready():
                return
            time.sleep(0.1)
        raise WebHarnessError("服务器直连代理未能在 127.0.0.1:18889 启动")

    def models(self, template: dict) -> list[dict]:
        if not self.status().get("ready"):
            return []
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / "native-model-template.json"
        with self._lock:
            temporary = path.with_suffix(".next")
            temporary.write_text(json.dumps(template), encoding="utf-8")
            os.replace(temporary, path)
            value = self._json("/bridge/models")
        return [row for row in value.get("models", [])
                if isinstance(row, dict) and is_web_model(row.get("slug"))]

    def refresh_catalog(self) -> dict:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        provider = config.get("model_provider")
        if provider not in {"browser_ai_bridge", "server_codex_executor"}:
            raise WebHarnessError("请先选择 Hybrid 或服务器中心模式")
        target_value = config.get("model_catalog_json")
        if not isinstance(target_value, str):
            raise WebHarnessError("请先生成 Hybrid 模型目录，再刷新网页模型")
        target = (codex_home / target_value).resolve()
        target.relative_to(codex_home)
        raw = target.read_bytes()
        catalog = json.loads(raw)
        native = [row for row in catalog.get("models", [])
                  if isinstance(row, dict) and not is_web_model(row.get("slug"))]
        rows = self.models({"models": native})
        if not rows:
            raise WebHarnessError("执行器尚未准备好；请完成登录和模式配置后重试")
        catalog["models"] = native + rows
        # Compare before replacing so a concurrently selected CC provider is
        # never overwritten by this catalog-only operation.
        if target.read_bytes() != raw:
            raise WebHarnessError("模型目录正在更新，请稍后重试")
        backup = target.with_suffix(target.suffix + ".before-web")
        if not backup.exists():
            backup.write_bytes(raw)
        temporary = target.with_suffix(target.suffix + ".web-next")
        temporary.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
        cache = codex_home / "browser-ai-bridge-web-models.json"
        cache_next = cache.with_suffix(".next")
        cache_next.write_text(json.dumps({"models": rows}, ensure_ascii=False), encoding="utf-8")
        os.replace(cache_next, cache)
        return {"models": len(rows), "restart_required": True}

    def configure_network(self, mode: str) -> dict:
        if mode not in {"system", "gateway"}:
            raise WebHarnessError("Unsupported WebHarness network mode")
        if self.status().get("running"):
            raise WebHarnessError("请先退出 WebHarness，再切换网络")
        self._write_network(mode)
        return {"network": mode, "restart_required": True}


def relay_web_response(handler, method: str, suffix: str, body: bytes) -> None:
    """Stream without buffering SSE and without forwarding Codex login secrets."""
    if method != "POST" or suffix not in {"/responses", "/responses/compact"}:
        raise WebHarnessError("Unsupported WebHarness endpoint")
    connection = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=600)
    started = False
    finished = threading.Event()
    def watch_disconnect() -> None:
        while not finished.wait(0.25):
            try:
                readable, _, _ = select.select([handler.connection], [], [], 0)
                if readable and not handler.connection.recv(1, socket.MSG_PEEK):
                    if connection.sock:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    connection.close()
                    return
            except OSError:
                connection.close()
                return
    if isinstance(getattr(handler, "connection", None), socket.socket):
        threading.Thread(target=watch_disconnect, name="web-turn-cancellation", daemon=True).start()
    try:
        headers = {name: value for name, value in handler.headers.items()
                   if name.lower() not in _HOP_HEADERS
                   and name.lower() not in {part.strip().lower() for part in handler.headers.get("Connection", "").split(",")}}
        headers["X-Bridge-Web-Token"] = runtime_token()
        connection.request(method, "/bridge/v1" + suffix, body, headers)
        response = connection.getresponse()
        handler.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in _HOP_HEADERS:
                handler.send_header(name, value)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        started = True
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            handler.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            handler.wfile.flush()
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except (OSError, ValueError, WebHarnessError, http.client.HTTPException):
        if not started:
            payload = json.dumps({"error": {"code": "web_harness_unavailable",
                "message": "网页执行器未就绪，请在 Bridge 中打开网页执行器完成登录和配置"}}).encode()
            handler.send_response(503)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        handler.close_connection = True
    finally:
        finished.set()
        connection.close()
