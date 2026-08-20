"""Safe local source updates requested by the paired Chrome extensions.

Chrome extensions cannot write their own unpacked directory.  The Native Host
is the deliberately small, local privilege boundary that receives a GitHub
archive *through Chrome*, validates it, and updates a known project checkout.
No account tokens, browser cookies, or arbitrary URLs cross this interface.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


PROJECT_BRIDGE = "fanvpn-bridge"
PROJECT_GATEWAY = "browser-gateway"
SUPPORTED_PROJECTS = frozenset({PROJECT_BRIDGE, PROJECT_GATEWAY})
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_FILES = 20_000
_STATE_NAME = "installation.json"


class UpdateControlError(RuntimeError):
    """A safe, user-facing software update failure."""


class LocalUpdateController:
    """Stages a trusted GitHub source archive and applies it locally."""

    def __init__(
        self,
        *,
        cache_base: Path,
        runtime_root: Path | None = None,
        documents: Path | None = None,
        powershell_path: Path | None = None,
        python_path: Path | None = None,
        timeout_seconds: float = 20 * 60,
    ) -> None:
        self._cache_base = cache_base.resolve()
        self._state_path = self._cache_base / _STATE_NAME
        self._runtime_root = (runtime_root or _infer_runtime_root()).resolve()
        self._documents = (documents or Path.home() / "Documents").resolve()
        self._powershell_path = (
            powershell_path or Path(os.environ.get("SystemRoot", r"C:\\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        ).resolve()
        self._python_path = python_path
        self._timeout_seconds = timeout_seconds

    def status(self) -> dict[str, object]:
        state = self._read_state()
        return {
            "bridge_root": str(self._project_root(PROJECT_BRIDGE, state)),
            "gateway_root": str(self._project_root(PROJECT_GATEWAY, state)),
            "default_bridge_root": str(self._documents / PROJECT_BRIDGE),
            "default_gateway_root": str(self._documents / PROJECT_GATEWAY),
        }

    def apply_archive(
        self,
        *,
        project: str,
        archive: Path,
        commit: str,
        install_root: str | None = None,
    ) -> dict[str, object]:
        if project not in SUPPORTED_PROJECTS:
            raise UpdateControlError("Unsupported update project")
        if not _is_commit(commit):
            raise UpdateControlError("Update package identity is invalid")
        if not archive.is_file() or archive.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise UpdateControlError("Update package is missing or too large")

        state = self._read_state()
        previous_root = self._project_root(project, state)
        root = self._select_root(project, state, install_root)
        extracted = self._extract_archive(project, archive, commit)
        try:
            _replace_project_source(extracted, root, project)
        finally:
            shutil.rmtree(extracted.parent, ignore_errors=True)

        state[project] = str(root)
        self._write_state(state)
        if project == PROJECT_BRIDGE:
            self._rebuild_bridge(root)
        return {
            "project": project,
            "commit": commit,
            "install_root": str(root),
            # Moving an unpacked extension to a new root is possible, but
            # Chrome intentionally requires the user to approve that root
            # once in chrome://extensions.  In-place updates reload normally.
            "extension_rebind_required": not _same_path(root, previous_root),
        }

    def _project_root(self, project: str, state: dict[str, str]) -> Path:
        configured = state.get(project)
        if configured:
            return Path(configured).expanduser().resolve()
        if project == PROJECT_BRIDGE:
            return self._runtime_root
        return (self._documents / PROJECT_GATEWAY).resolve()

    def _select_root(self, project: str, state: dict[str, str], requested: str | None) -> Path:
        if requested is None or not requested.strip():
            root = self._project_root(project, state)
        else:
            root = Path(requested).expanduser().resolve()
        if root.parent == root or str(root) in {root.anchor, ""}:
            raise UpdateControlError("Installation directory must be a project folder, not a drive root")
        return root

    def _extract_archive(self, project: str, archive: Path, commit: str) -> Path:
        scratch = Path(tempfile.mkdtemp(prefix=f"browser-ai-update-{project}-"))
        try:
            with zipfile.ZipFile(archive) as package:
                entries = package.infolist()
                if len(entries) > _MAX_ARCHIVE_FILES:
                    raise UpdateControlError("Update package contains too many files")
                roots = {Path(item.filename).parts[0] for item in entries if item.filename and not item.is_dir()}
                if len(roots) != 1:
                    raise UpdateControlError("Update package layout is invalid")
                top = next(iter(roots))
                expected_prefix = f"{project}-{commit[:7]}"
                if not top.startswith(expected_prefix):
                    raise UpdateControlError("Update package does not match the requested revision")
                for item in entries:
                    target = (scratch / item.filename).resolve()
                    if not _is_within(target, scratch) or _zip_entry_is_link(item):
                        raise UpdateControlError("Update package contains an unsafe path")
                package.extractall(scratch)
            source = scratch / top
            if not (source / "README.md").is_file() or not source.is_dir():
                raise UpdateControlError("Update package has no project source")
            return source
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise UpdateControlError("Update package could not be unpacked") from exc

    def _rebuild_bridge(self, root: Path) -> None:
        script = root / "tools" / "update_native_host.ps1"
        if not script.is_file() or not self._powershell_path.is_file():
            raise UpdateControlError("Bridge update tools are incomplete")
        python = self._find_python()
        if python is None:
            raise UpdateControlError("Python 3.12+ was not found; install it once, then retry the update")
        command = [
            str(self._powershell_path), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(script), "-Python", str(python),
        ]
        try:
            completed = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self._timeout_seconds, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateControlError("Native Host update stopped unexpectedly") from exc
        if completed.returncode != 0:
            raise UpdateControlError(_friendly_build_failure(completed.stdout + b"\n" + completed.stderr))

    def _find_python(self) -> Path | None:
        candidates = [self._python_path] if self._python_path else []
        command = shutil.which("python")
        if command:
            candidates.append(Path(command))
        for candidate in candidates:
            if candidate is None or not candidate.is_file():
                continue
            try:
                result = subprocess.run(
                    [str(candidate), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                continue
            if result.returncode == 0:
                return candidate.resolve()
        return None

    def _read_state(self) -> dict[str, str]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            project: value for project, value in data.items()
            if project in SUPPORTED_PROJECTS and isinstance(value, str) and value.strip()
        }

    def _write_state(self, state: dict[str, str]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".next")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self._state_path)


def _replace_project_source(source: Path, destination: Path, project: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    allowed = _project_entries(project)
    # Only replace version-controlled application entries.  Build slots, the
    # user's .git directory and local caches survive an update by design.
    for entry in allowed:
        target = destination / entry
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for entry in allowed:
        source_entry = source / entry
        if not source_entry.exists():
            continue
        target = destination / entry
        if source_entry.is_dir():
            shutil.copytree(source_entry, target)
        else:
            shutil.copy2(source_entry, target)


def _project_entries(project: str) -> tuple[str, ...]:
    if project == PROJECT_BRIDGE:
        return (
            ".github", "chrome-extension", "config", "contracts", "docs", "native-host", "tests", "tools",
            ".gitignore", "install.ps1", "uninstall.ps1", "README.md",
        )
    return (
        "docs", "extension", "server", "tools", ".gitignore", "LICENSE", "README.md", "package.json",
    )


def _infer_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        # <root>/dist-a/browser-ai-bridge/browser-ai-bridge.exe
        if executable.parent.name == "browser-ai-bridge" and executable.parent.parent.name.startswith("dist-"):
            return executable.parents[2]
    return Path(__file__).resolve().parents[2]


def _is_commit(value: str) -> bool:
    return 7 <= len(value) <= 64 and all(character in "0123456789abcdef" for character in value.lower())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _zip_entry_is_link(entry: zipfile.ZipInfo) -> bool:
    return (entry.external_attr >> 16) & 0o170000 == 0o120000


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))


def _friendly_build_failure(output: bytes) -> str:
    text = output.decode("utf-8", errors="replace").casefold()
    if "permissionerror" in text or "拒绝访问" in text:
        return "Native Host files are still in use; close Chrome and retry the update"
    if "python 3.12+" in text:
        return "Python 3.12+ is required to update this Bridge"
    return "Native Host build or registration failed; the previous registered version was kept"
