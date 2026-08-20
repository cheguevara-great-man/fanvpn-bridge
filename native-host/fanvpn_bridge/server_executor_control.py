"""Safe switching between the legacy Bridge and server-centered Codex paths."""

from __future__ import annotations

import json
import os
from http.client import HTTPConnection
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable

from .server_client import (
    ServerClientError,
    default_server_client_config_path,
    load_server_client_config,
    write_server_client_config,
)


MODE_BROWSER_CHAIN = "browser_chain"
MODE_SERVER_CENTER = "server_center"
SUPPORTED_SERVER_EXECUTOR_MODES = frozenset({MODE_BROWSER_CHAIN, MODE_SERVER_CENTER})
_SERVER_PROVIDER = "server_codex_executor"
_LOCAL_PORT = 18890
_BROWSER_BRIDGE_URL = "http://127.0.0.1:18888/server-executor"
_STATE_NAME = "server-executor-ui.json"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class ServerExecutorControlError(RuntimeError):
    """A concise, user-facing failure from the server executor switch."""


class ServerExecutorTransportController:
    """Own only the extra 18890 client; never stop Chrome's Native Host."""

    def __init__(
        self,
        *,
        cache_base: Path | None = None,
        codex_home: Path | None = None,
        client_config_path: Path | None = None,
        process_starter: Callable[[list[str]], None] | None = None,
        process_stopper: Callable[[], None] | None = None,
        readiness_probe: Callable[[], bool] | None = None,
    ) -> None:
        home = Path.home()
        local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        self._cache_base = (cache_base or local_appdata / "FanVPNBridge").resolve()
        self._codex_home = (codex_home or Path(os.environ.get("CODEX_HOME", home / ".codex"))).resolve()
        self._client_config_path = (client_config_path or default_server_client_config_path()).resolve()
        self._state_path = self._cache_base / _STATE_NAME
        self._process_starter = process_starter or self._start_process
        self._process_stopper = process_stopper or self._stop_existing_client
        self._readiness_probe = readiness_probe or self._is_client_ready

    def get_state(self) -> dict[str, object]:
        configured = False
        transport: str | None = None
        try:
            config = load_server_client_config(self._client_config_path)
            configured = True
            transport = config.transport
        except ServerClientError:
            pass
        mode = MODE_SERVER_CENTER if self._current_provider() == _SERVER_PROVIDER else MODE_BROWSER_CHAIN
        return {
            "mode": mode,
            "configured": configured,
            "transport": transport,
            "client_running": self._readiness_probe(),
            "local_port": _LOCAL_PORT,
        }

    def set_mode(self, mode: str) -> dict[str, object]:
        if mode not in SUPPORTED_SERVER_EXECUTOR_MODES:
            raise ServerExecutorControlError("Unsupported server transport mode")
        if mode == MODE_BROWSER_CHAIN:
            self._switch_to_browser_chain()
        else:
            self._switch_to_server_center()
        state = self.get_state()
        state["restart_vscode_required"] = True
        return state

    def _switch_to_server_center(self) -> None:
        try:
            config = load_server_client_config(self._client_config_path)
        except ServerClientError as exc:
            raise ServerExecutorControlError(
                "服务器中心尚未配置：先在 Browser Gateway 网页注册这台设备"
            ) from exc
        self._remember_previous_provider()
        write_server_client_config(
            self._client_config_path,
            executor_url=config.executor_url,
            device_token=config.device_token,
            local_token=config.local_token,
            transport="browser",
            browser_bridge_url=_BROWSER_BRIDGE_URL,
        )
        self._process_stopper()
        self._process_starter(self._client_command())
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if self._readiness_probe():
                self._set_provider(_SERVER_PROVIDER)
                return
            time.sleep(0.1)
        raise ServerExecutorControlError(
            "服务器中心客户端未启动；请确认 Chrome、Browser Gateway 与 AI Bridge 都已连接"
        )

    def _switch_to_browser_chain(self) -> None:
        self._process_stopper()
        previous = self._load_state().get("previous_model_provider")
        provider = previous if isinstance(previous, str) and previous else "browser_ai_bridge"
        self._set_provider(provider)

    def _remember_previous_provider(self) -> None:
        current = self._current_provider()
        if current and current != _SERVER_PROVIDER:
            state = self._load_state()
            state["previous_model_provider"] = current
            self._write_state(state)

    def _current_provider(self) -> str | None:
        path = self._codex_home / "config.toml"
        try:
            if path.stat().st_size > _MAX_CONFIG_BYTES:
                return None
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        first_table = re.search(r"(?m)^\s*\[", content)
        top = content[: first_table.start()] if first_table else content
        found = re.search(r'(?m)^\s*model_provider\s*=\s*"(?P<value>[^"]+)"\s*$', top)
        return found.group("value") if found else None

    def _set_provider(self, provider: str) -> None:
        path = self._codex_home / "config.toml"
        try:
            original = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeError) as exc:
            raise ServerExecutorControlError("Codex configuration could not be read") from exc
        first_table = re.search(r"(?m)^\s*\[", original)
        top_end = first_table.start() if first_table else len(original)
        top = original[:top_end]
        tail = original[top_end:]
        pattern = r'(?m)^\s*model_provider\s*=\s*"[^"]+"\s*$'
        if re.search(pattern, top):
            top = re.sub(pattern, f'model_provider = "{provider}"', top, count=1)
        else:
            top = f'model_provider = "{provider}"\n' + top
        if provider == _SERVER_PROVIDER:
            tail = re.sub(
                r'(?ms)^\s*\[model_providers\.server_codex_executor\]\s*.*?(?=^\s*\[|\Z)',
                "",
                tail,
            )
            if tail and not tail.startswith("\n"):
                tail = "\n" + tail
            tail += (
                "\n# BEGIN Browser AI Bridge server executor\n"
                "[model_providers.server_codex_executor]\n"
                "name = \"Server-side Codex Executor (browser transport)\"\n"
                "base_url = \"http://127.0.0.1:18890/v1\"\n"
                "requires_openai_auth = false\n"
                "wire_api = \"responses\"\n"
                "supports_websockets = false\n"
                "# END Browser AI Bridge server executor\n"
            )
        content = top.rstrip() + "\n" + tail.lstrip("\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.server-executor-{os.getpid()}.next")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ServerExecutorControlError("Codex configuration could not be updated") from exc

    def _load_state(self) -> dict[str, object]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _write_state(self, value: dict[str, object]) -> None:
        self._cache_base.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".json.next")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._state_path)

    def _client_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [str(Path(sys.executable).resolve()), "--server-client", "--server-client-config", str(self._client_config_path), "--server-client-port", str(_LOCAL_PORT)]
        return [sys.executable, "-m", "fanvpn_bridge.main", "--server-client", "--server-client-config", str(self._client_config_path), "--server-client-port", str(_LOCAL_PORT)]

    @staticmethod
    def _start_process(command: list[str]) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)

    @staticmethod
    def _stop_existing_client() -> None:
        if os.name != "nt":
            return
        # The matching arguments identify only the separate 18890 process.
        # The Chrome Native Host never has --server-client, so it is not touched.
        script = (
            "$p=Get-CimInstance Win32_Process -Filter \"Name='browser-ai-bridge.exe'\" | "
            "Where-Object {$_.CommandLine -match '(?:^|\\s)--server-client(?:\\s|$)' -and "
            "$_.CommandLine -match '(?:^|\\s)--server-client-port\\s+18890(?:\\s|$)'}; "
            "$p | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}"
        )
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @staticmethod
    def _is_client_ready() -> bool:
        try:
            connection = HTTPConnection("127.0.0.1", _LOCAL_PORT, timeout=0.4)
            connection.request("GET", "/ready")
            response = connection.getresponse()
            body = response.read(1024)
            connection.close()
            return response.status == 200 and b'"server-client"' in body
        except OSError:
            return False
