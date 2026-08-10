"""Safe local device enrollment configuration written through Native Messaging."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit


class DeviceConfigError(RuntimeError):
    pass


class DeviceConfigController:
    def __init__(self, runtime_directory: Path) -> None:
        self._runtime_directory = runtime_directory
        self._path = runtime_directory / "usage-reporting.json"

    def status(self) -> dict[str, object]:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"configured": False}
        machine_id = value.get("machine_id")
        machine_name = value.get("machine_name")
        return {
            "configured": True,
            "machine_id": machine_id if isinstance(machine_id, str) else "",
            "machine_name": machine_name if isinstance(machine_name, str) else "",
            "dashboard_url": value.get("dashboard_url", ""),
        }

    def apply(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise DeviceConfigError("Device configuration is missing")
        machine_id = _string(value, "machine_id", 64)
        try:
            machine_id = str(uuid.UUID(machine_id))
        except ValueError as error:
            raise DeviceConfigError("Invalid machine ID") from error
        machine_name = _string(value, "machine_name", 128).strip()
        if not machine_name or any(ord(character) < 32 for character in machine_name):
            raise DeviceConfigError("Invalid machine name")
        collector_url = _https_url(value, "collector_url", required_path="/v1/usage/events")
        dashboard_url = _https_url(value, "dashboard_url", required_path="/dashboard")
        report_token = _string(value, "report_token", 512)
        if len(report_token) < 20 or any(character.isspace() for character in report_token):
            raise DeviceConfigError("Invalid report token")
        document = {
            "collector_url": collector_url,
            "report_token": report_token,
            "machine_id": machine_id,
            "machine_name": machine_name,
            "dashboard_url": dashboard_url,
        }
        self._runtime_directory.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.next")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        os.replace(temporary, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return {**self.status(), "restart_required": True}


def _string(value: dict[str, object], name: str, maximum: int) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not 1 <= len(item) <= maximum or "\0" in item:
        raise DeviceConfigError(f"Invalid {name}")
    return item


def _https_url(value: dict[str, object], name: str, *, required_path: str) -> str:
    item = _string(value, name, 1024).rstrip("/")
    parsed = urlsplit(item)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path != required_path
    ):
        raise DeviceConfigError(f"Invalid {name}")
    return item
