from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from fanvpn_bridge.update_control import LocalUpdateController, UpdateControlError


COMMIT = "abcdef1234567890abcdef1234567890abcdef12"


def _archive(path: Path, project: str, *, unsafe: bool = False) -> Path:
    top = f"{project}-{COMMIT}"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr(f"{top}/README.md", "updated")
        if project == "browser-gateway":
            package.writestr(f"{top}/extension/manifest.json", '{"version":"9.9.9"}')
        else:
            package.writestr(f"{top}/chrome-extension/manifest.json", '{"version":"9.9.9"}')
        if unsafe:
            package.writestr(f"{top}/../../escape.txt", "no")
    return path


def test_gateway_update_replaces_only_project_files_and_remembers_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "fanvpn-bridge"
    runtime.mkdir()
    gateway = tmp_path / "custom-gateway"
    gateway.mkdir()
    (gateway / "unrelated.txt").write_text("keep", encoding="utf-8")
    controller = LocalUpdateController(
        cache_base=tmp_path / "cache", runtime_root=runtime, documents=tmp_path / "Documents"
    )

    result = controller.apply_archive(
        project="browser-gateway", archive=_archive(tmp_path / "gateway.zip", "browser-gateway"),
        commit=COMMIT, install_root=str(gateway),
    )

    assert result["extension_rebind_required"] is True
    assert (gateway / "extension" / "manifest.json").is_file()
    assert (gateway / "unrelated.txt").read_text(encoding="utf-8") == "keep"
    assert controller.status()["gateway_root"] == str(gateway.resolve())


def test_update_rejects_unsafe_archive_paths(tmp_path: Path) -> None:
    controller = LocalUpdateController(
        cache_base=tmp_path / "cache", runtime_root=tmp_path / "runtime", documents=tmp_path / "Documents"
    )
    with pytest.raises(UpdateControlError, match="unsafe path"):
        controller.apply_archive(
            project="browser-gateway", archive=_archive(tmp_path / "unsafe.zip", "browser-gateway", unsafe=True),
            commit=COMMIT,
        )
