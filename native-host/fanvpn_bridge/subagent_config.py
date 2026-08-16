"""Safe editor for the Bridge-owned portion of Codex subagent configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Mapping

from .subagent_policy import POLICY_CONFIGURED, SubagentPolicyConfig, SubagentPolicyStore


_MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
_MANAGED_ROLE_PREFIX = "browser-ai-bridge-"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class SubagentConfigError(RuntimeError):
    pass


class SubagentConfigurationController:
    def __init__(self, codex_home: Path, policy_store: SubagentPolicyStore) -> None:
        self._home = codex_home.resolve()
        self._policy = policy_store

    def status(self) -> dict[str, object]:
        policy = self._policy.read()
        models = self._models()
        return {
            "mode": policy.mode,
            "default_model": policy.default_model,
            "default_reasoning_effort": policy.default_reasoning_effort,
            "models": models,
            "roles": self._roles(),
        }

    def apply(self, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise SubagentConfigError("Subagent settings must be an object")
        model = str(value.get("default_model") or "").strip()
        effort = str(value.get("default_reasoning_effort") or "").strip().lower()
        if not _MODEL_RE.fullmatch(model):
            raise SubagentConfigError("Invalid default subagent model")
        if effort not in _EFFORTS:
            raise SubagentConfigError("Invalid default subagent reasoning effort")
        roles_value = value.get("roles", [])
        if not isinstance(roles_value, list) or len(roles_value) > 12:
            raise SubagentConfigError("At most 12 managed roles are allowed")
        roles = [self._validate_role(item) for item in roles_value]
        self._update_config(model, effort)
        self._write_roles(roles)
        self._policy.write(SubagentPolicyConfig(POLICY_CONFIGURED, model, effort))
        return self.status()

    def _models(self) -> list[dict[str, object]]:
        path = self._home / "browser-ai-bridge-gemini-models.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            models = value.get("models") if isinstance(value, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            models = None
        result: list[dict[str, object]] = []
        if isinstance(models, list):
            for item in models[:200]:
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("slug") or "")
                if not _MODEL_RE.fullmatch(slug):
                    continue
                efforts = []
                for raw in item.get("supported_reasoning_levels") or []:
                    candidate = raw.get("effort") if isinstance(raw, dict) else raw
                    candidate = str(candidate or "").lower()
                    if candidate in _EFFORTS and candidate not in efforts:
                        efforts.append(candidate)
                result.append({
                    "id": slug,
                    "name": str(item.get("display_name") or slug)[:160],
                    "efforts": efforts or ["low", "medium", "high"],
                })
        return result

    def _roles(self) -> list[dict[str, str]]:
        directory = self._home / "agents"
        roles: list[dict[str, str]] = []
        paths = []
        if directory.is_dir():
            paths = list(directory.glob(f"{_MANAGED_ROLE_PREFIX}*.toml")) + list(
                directory.glob(f"{_MANAGED_ROLE_PREFIX}*.toml.disabled")
            )
        for path in sorted(paths):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            role = {key: _toml_string(text, key) for key in (
                "name", "description", "developer_instructions", "model", "model_reasoning_effort"
            )}
            if _ROLE_RE.fullmatch(role["name"]):
                roles.append(role)
        return roles

    def _validate_role(self, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise SubagentConfigError("Each role must be an object")
        role = {
            "name": str(value.get("name") or "").strip().lower(),
            "description": str(value.get("description") or "").strip(),
            "developer_instructions": str(value.get("developer_instructions") or "").strip(),
            "model": str(value.get("model") or "").strip(),
            "model_reasoning_effort": str(value.get("model_reasoning_effort") or "").strip().lower(),
        }
        if not _ROLE_RE.fullmatch(role["name"]):
            raise SubagentConfigError("Role names must use lowercase letters, digits, and underscores")
        if not role["description"] or len(role["description"]) > 500:
            raise SubagentConfigError("Each role needs a short description")
        if not role["developer_instructions"] or len(role["developer_instructions"]) > 8000:
            raise SubagentConfigError("Each role needs developer instructions")
        if not _MODEL_RE.fullmatch(role["model"]):
            raise SubagentConfigError("Each role needs a valid model")
        if role["model_reasoning_effort"] not in _EFFORTS:
            raise SubagentConfigError("Each role needs a valid reasoning effort")
        return role

    def _update_config(self, model: str, effort: str) -> None:
        path = self._home / "config.toml"
        try:
            if path.is_file() and path.stat().st_size > _MAX_CONFIG_BYTES:
                raise SubagentConfigError("Codex config.toml is too large")
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except (OSError, UnicodeError) as exc:
            raise SubagentConfigError("Codex config.toml could not be read") from exc
        marker = "# BEGIN Browser AI Bridge managed subagent defaults"
        if marker not in text:
            raise SubagentConfigError("Select Hybrid configured mode before editing its defaults")
        text = _set_table_key(text, "agents", "default_subagent_model", f'default_subagent_model = "{model}"')
        text = _set_table_key(
            text, "agents", "default_subagent_reasoning_effort",
            f'default_subagent_reasoning_effort = "{effort}"',
        )
        _atomic_text(path, text)

    def _write_roles(self, roles: list[dict[str, str]]) -> None:
        directory = self._home / "agents"
        directory.mkdir(parents=True, exist_ok=True)
        desired: set[Path] = set()
        for role in roles:
            path = directory / f"{_MANAGED_ROLE_PREFIX}{role['name'].replace('_', '-')}.toml"
            desired.add(path)
            lines = [
                f'name = {_toml_quote(role["name"])}',
                f'description = {_toml_quote(role["description"])}',
                f'developer_instructions = {_toml_quote(role["developer_instructions"])}',
                f'model = {_toml_quote(role["model"])}',
                f'model_reasoning_effort = {_toml_quote(role["model_reasoning_effort"])}',
                "",
            ]
            _atomic_text(path, "\n".join(lines))
        for path in directory.glob(f"{_MANAGED_ROLE_PREFIX}*.toml"):
            if path not in desired:
                path.unlink(missing_ok=True)
        for path in directory.glob(f"{_MANAGED_ROLE_PREFIX}*.toml.disabled"):
            active = path.with_name(path.name.removesuffix(".disabled"))
            if active not in desired:
                path.unlink(missing_ok=True)


def _set_table_key(text: str, table: str, key: str, line: str) -> str:
    table_match = re.search(
        rf"(?ms)^\s*\[{re.escape(table)}\]\s*(?:\r?\n|$)(?P<body>.*?)(?=^\s*\[|\Z)", text
    )
    if not table_match:
        return text.rstrip() + f"\n\n[{table}]\n{line}\n"
    body = table_match.group("body")
    key_match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=.*(?:\r?\n|$)", body)
    if key_match:
        body = body[: key_match.start()] + line + "\n" + body[key_match.end() :]
    else:
        body = line + "\n" + body
    return text[: table_match.start("body")] + body + text[table_match.end("body") :]


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_string(text: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(?P<value>\"(?:\\.|[^\"])*\")\s*$", text)
    if not match:
        return ""
    try:
        return str(json.loads(match.group("value")))
    except json.JSONDecodeError:
        return ""


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
