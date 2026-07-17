"""Restricted Codex network-mode control for the paired Chrome extension."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from contextlib import contextmanager
import msvcrt


MODE_DIRECT = "direct"
MODE_BROWSER_LEAN = "browser_lean"
MODE_BROWSER_FULL = "browser_full"
MODE_UNMANAGED = "unmanaged"
SUPPORTED_MODES = frozenset({MODE_DIRECT, MODE_BROWSER_LEAN, MODE_BROWSER_FULL})
_SCRIPT_MODES = {
    MODE_DIRECT: "Direct",
    MODE_BROWSER_LEAN: "BrowserLean",
    MODE_BROWSER_FULL: "BrowserFull",
}
_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class ModeControlError(RuntimeError):
    """A safe, user-facing mode-control failure."""


class CodexModeController:
    """Read and atomically switch only the three supported Codex modes."""

    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        settings_path: Path | None = None,
        state_path: Path | None = None,
        tools_directory: Path | None = None,
        powershell_path: Path | None = None,
        timeout_seconds: float = 45,
    ) -> None:
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        self._codex_home = (codex_home or Path(os.environ.get("CODEX_HOME", home / ".codex"))).resolve()
        self._settings_path = (
            settings_path or appdata / "Code" / "User" / "settings.json"
        ).resolve()
        self._state_path = (
            state_path or local_appdata / "FanVPNBridge" / "vscode-codex-endpoint.json"
        ).resolve()
        self._tools_directory = (tools_directory or _default_tools_directory()).resolve()
        self._powershell_path = (powershell_path or _default_powershell_path()).resolve()
        self._timeout_seconds = timeout_seconds
        self._lock_path = self._state_path.parent / "vscode-mode-switch.lock"

    def get_mode(self) -> str:
        config_path = self._codex_home / "config.toml"
        try:
            if config_path.stat().st_size > _MAX_CONFIG_BYTES:
                return MODE_UNMANAGED
            content = config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return MODE_UNMANAGED
        first_table = re.search(r"(?m)^\s*\[", content)
        top = content[: first_table.start()] if first_table else content
        provider_match = re.search(
            r'(?m)^\s*model_provider\s*=\s*"(?P<value>[^"]+)"\s*$',
            top,
        )
        if provider_match is None:
            return MODE_UNMANAGED
        provider = provider_match.group("value")
        if provider == "browser_ai_direct":
            return MODE_DIRECT
        if provider != "browser_ai_bridge":
            return MODE_UNMANAGED
        if re.search(
            r'(?m)^\s*chatgpt_base_url\s*=\s*"http://127\.0\.0\.1:18888/chatgpt-backend/',
            top,
        ):
            return MODE_BROWSER_FULL
        return MODE_BROWSER_LEAN

    def set_mode(self, mode: str) -> str:
        if mode not in SUPPORTED_MODES:
            raise ModeControlError("Unsupported Codex mode")
        script = self._tools_directory / "start_vscode_network_mode.ps1"
        if not script.is_file():
            raise ModeControlError("Mode launcher is missing; update the Native Host")
        if not self._powershell_path.is_file():
            raise ModeControlError("Windows PowerShell could not be found")
        command = [
            str(self._powershell_path),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Mode",
            _SCRIPT_MODES[mode],
            "-CodexHome",
            str(self._codex_home),
            "-SettingsPath",
            str(self._settings_path),
            "-StatePath",
            str(self._state_path),
        ]
        managed_paths = (
            self._codex_home / "config.toml",
            Path(str(self._codex_home / "config.toml") + ".before-network-mode.bak"),
            self._settings_path,
            Path(str(self._settings_path) + ".before-network-mode.bak"),
            self._state_path,
        )
        with _exclusive_switch(self._lock_path):
            snapshots = _snapshot_files(managed_paths)
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self._timeout_seconds,
                    check=False,
                    creationflags=creation_flags,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                _restore_files(snapshots)
                raise ModeControlError(
                    "Mode switch stopped unexpectedly; configuration was restored"
                ) from exc
            if completed.returncode == 23:
                _restore_files(snapshots)
                raise ModeControlError(
                    "Close every VS Code window, wait a few seconds, then click the mode again"
                )
            if completed.returncode != 0:
                _restore_files(snapshots)
                raise ModeControlError(_friendly_failure(completed.stdout, completed.stderr))
            actual = self.get_mode()
            if actual != mode:
                _restore_files(snapshots)
                raise ModeControlError("Mode switch completed but verification failed")
            return actual


def _default_tools_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "tools"
    return Path(__file__).resolve().parents[2] / "tools"


def _default_powershell_path() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _friendly_failure(stdout: bytes, stderr: bytes) -> str:
    detail = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    known_failures = (
        (
            "Direct mode is not configured",
            "Direct mode is not configured; run tools\\install_vscode_direct_mode.ps1 first",
        ),
        (
            "product API bridge is not ready",
            "Browser mode is not ready; verify Chrome, Browser Gateway and the Bridge are connected",
        ),
        (
            "VS Code settings JSON cannot be updated safely",
            "VS Code settings.json is invalid and could not be updated safely",
        ),
        ("Visual Studio Code was not found", "Visual Studio Code could not be found"),
        (
            "local direct proxy did not become ready",
            "The Direct proxy did not become ready on 127.0.0.1:18889",
        ),
    )
    for marker, message in known_failures:
        if marker.casefold() in detail.casefold():
            return message
    return "Mode launch failed; configuration was restored automatically"


@contextmanager
def _exclusive_switch(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ModeControlError("Another VS Code mode switch is already running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _snapshot_files(paths: tuple[Path, ...]) -> tuple[tuple[Path, bytes | None], ...]:
    snapshots: list[tuple[Path, bytes | None]] = []
    for path in paths:
        try:
            snapshots.append((path, path.read_bytes() if path.is_file() else None))
        except OSError as exc:
            raise ModeControlError(f"Managed configuration could not be read: {path.name}") from exc
    return tuple(snapshots)


def _restore_files(snapshots: tuple[tuple[Path, bytes | None], ...]) -> None:
    for path, content in snapshots:
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f"{path.name}.restore.{os.getpid()}")
                temporary.write_bytes(content)
                os.replace(temporary, path)
        except OSError:
            # The PowerShell transaction is the primary rollback. This second
            # layer is best-effort protection for a killed/timed-out launcher.
            continue
