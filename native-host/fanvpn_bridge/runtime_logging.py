"""Secret-free rotating runtime logs for startup and transport state."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_runtime_logging() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base_directory = (
        Path(local_app_data) / "FanVPNBridge"
        if local_app_data
        else Path.home() / ".fanvpn-bridge"
    )
    base_directory.mkdir(parents=True, exist_ok=True)
    log_path = base_directory / "fanvpn-bridge.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s level=%(levelname)s component=%(name)s event=%(message)s"
        )
    )
    root = logging.getLogger("fanvpn_bridge")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    return log_path
