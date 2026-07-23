"""One-click local setup for the Antigravity VS Code companion."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import winreg


_CLI_SETTING = "antigravity.cliPath"
_CLOUD_CODE_URL = "http://127.0.0.1:18888/antigravity"


class AntigravitySetupError(RuntimeError):
    """A safe setup failure suitable for the extension popup."""


class AntigravitySetupController:
    def __init__(
        self,
        *,
        home: Path | None = None,
        appdata: Path | None = None,
        local_appdata: Path | None = None,
        tools_directory: Path | None = None,
        powershell_path: Path | None = None,
        timeout_seconds: float = 900,
    ) -> None:
        self._home = (home or Path.home()).resolve()
        self._appdata = (
            appdata or Path(os.environ.get("APPDATA", self._home / "AppData" / "Roaming"))
        ).resolve()
        self._local_appdata = (
            local_appdata
            or Path(os.environ.get("LOCALAPPDATA", self._home / "AppData" / "Local"))
        ).resolve()
        self._tools_directory = (tools_directory or _default_tools_directory()).resolve()
        self._powershell_path = (powershell_path or _default_powershell_path()).resolve()
        self._timeout_seconds = timeout_seconds

    def status(self) -> dict[str, object]:
        browser_cli = self._local_appdata / "agy" / "bin" / "agy-browser.exe"
        settings_path = self._appdata / "Code" / "User" / "settings.json"
        expected_cli = str(browser_cli.resolve())
        configured_cli = _read_vscode_cli_path(settings_path)
        extension_root = self._home / ".vscode" / "extensions"
        extension_installed = any(extension_root.glob("lyadhgod.antigravity-vscode-*"))
        environment_ready = _read_user_environment("CLOUD_CODE_URL") == _CLOUD_CODE_URL
        state: dict[str, object] = {
            "cli_installed": browser_cli.is_file(),
            "extension_installed": extension_installed,
            "vscode_configured": configured_cli is not None
            and os.path.normcase(configured_cli) == os.path.normcase(expected_cli),
            "environment_configured": environment_ready,
            "restart_vscode_required": False,
        }
        state["ready"] = all(
            bool(state[name])
            for name in (
                "cli_installed",
                "extension_installed",
                "vscode_configured",
                "environment_configured",
            )
        )
        return state

    def setup(self) -> dict[str, object]:
        script = self._tools_directory / "setup_antigravity_vscode.ps1"
        if not script.is_file():
            raise AntigravitySetupError("Antigravity setup tool is missing; update the Native Host")
        if not self._powershell_path.is_file():
            raise AntigravitySetupError("Windows PowerShell could not be found")
        command = [
            str(self._powershell_path),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout_seconds,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AntigravitySetupError("Antigravity setup stopped unexpectedly") from exc
        output = (completed.stdout + b"\n" + completed.stderr).decode(
            "utf-8", errors="replace"
        )
        if completed.returncode != 0 or "BRIDGE_ANTIGRAVITY_SETUP_RESULT=READY" not in output:
            raise AntigravitySetupError(_friendly_failure(output))
        state = self.status()
        if not state["ready"]:
            raise AntigravitySetupError("Setup completed but local verification failed")
        state["restart_vscode_required"] = True
        return state


def _read_vscode_cli_path(path: Path) -> str | None:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig")).get(_CLI_SETTING)
        return value if isinstance(value, str) else None
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return None


def _read_user_environment(name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else None
    except OSError:
        return None


def _default_tools_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "tools"
    return Path(__file__).resolve().parents[2] / "tools"


def _default_powershell_path() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _friendly_failure(output: str) -> str:
    known = (
        ("missing route", "Bridge routes are outdated; update the Native Host first"),
        ("Visual Studio Code command line was not found", "Visual Studio Code was not found"),
        ("settings.json is not valid JSON", "VS Code settings.json could not be updated safely"),
        ("extension identity did not match", "VS Code extension security validation failed"),
        ("CLI installation failed", "Antigravity CLI installation failed"),
        ("extension installation failed", "Antigravity VS Code extension installation failed"),
    )
    folded = output.casefold()
    for marker, message in known:
        if marker.casefold() in folded:
            return message
    return "Antigravity setup failed; no credentials were changed"
