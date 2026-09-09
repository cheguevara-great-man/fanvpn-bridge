"""Hash-verified, versioned WebHarness installs; never overwrite a running slot."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
import zipfile

from .web_harness import WebHarnessController, WebHarnessError, runtime_home


def install_archive(archive: Path, expected_sha256: str, home: Path | None = None) -> dict:
    home = (home or runtime_home()).resolve()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise WebHarnessError("Invalid package checksum")
    if archive.stat().st_size > 512 * 1024 * 1024:
        raise WebHarnessError("WebHarness package exceeds 512 MiB")
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != expected_sha256:
            raise WebHarnessError("WebHarness package checksum mismatch")
    controller = WebHarnessController(home)
    if controller.status().get("running"):
        raise WebHarnessError("请先在 WebHarness 托盘菜单退出执行器，再安装或升级")
    home.mkdir(parents=True, exist_ok=True)
    versions = home / "versions"
    versions.mkdir(exist_ok=True)
    destination = versions / expected_sha256
    staging = Path(tempfile.mkdtemp(prefix="install-", dir=versions)).resolve()
    try:
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if len(entries) > 30000 or sum(item.file_size for item in entries) > 2 * 1024**3:
                raise WebHarnessError("WebHarness archive is too large")
            seen: set[str] = set()
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or not path.parts or any(
                    part in {".", ".."} or ":" in part or part.endswith((".", " "))
                    or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                    for part in path.parts
                ) or stat.S_ISLNK(entry.external_attr >> 16):
                    raise WebHarnessError("Unsafe package path")
                key = str(path).casefold()
                if key in seen:
                    raise WebHarnessError("Duplicate package path")
                seen.add(key)
                target = staging.joinpath(*path.parts).resolve()
                target.relative_to(staging)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
        if not (staging / "WebHarness.exe").is_file() or not (staging / "resources" / "app.asar").is_file():
            raise WebHarnessError("Package is not a WebHarness Windows distribution")
        if not destination.exists():
            # Windows scanners may briefly hold a newly extracted executable.
            # Keep the previous installation active while retrying that transient lock.
            deadline = time.monotonic() + 15
            while True:
                try:
                    staging.rename(destination)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.25)
        marker = home / "installation.json"
        previous = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
        value = {"directory": str(destination.relative_to(home)), "sha256": expected_sha256,
                 "previous_directory": previous.get("directory")}
        temporary = marker.with_suffix(".next")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, marker)
        return {"installed": True, "directory": str(destination), "restart_required": False}
    finally:
        # Only the verified temporary directory created above may be removed.
        if staging.exists():
            staging.relative_to(versions.resolve())
            shutil.rmtree(staging)
