"""Persistent, narrowly scoped model policy for Codex subagent requests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterable

from .contracts import Header


POLICY_NATIVE = "native"
POLICY_CONFIGURED = "configured"
POLICY_FORCE_GEMINI = "force_gemini_37_high"
SUPPORTED_POLICIES = frozenset({POLICY_NATIVE, POLICY_CONFIGURED, POLICY_FORCE_GEMINI})
_FORCEABLE_SUBAGENTS = frozenset({"collab_spawn", "review"})
_MAX_POLICY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class SubagentPolicyConfig:
    mode: str = POLICY_NATIVE
    default_model: str = "gemini-3.7-flash"
    default_reasoning_effort: str = "high"


@dataclass(frozen=True, slots=True)
class AppliedSubagentPolicy:
    payload: dict[str, Any]
    subagent_kind: str | None
    overridden: bool


class SubagentPolicyStore:
    """Atomic JSON store shared by mode-control and the loopback provider."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def read(self) -> SubagentPolicyConfig:
        with self._lock:
            try:
                if not self.path.is_file() or self.path.stat().st_size > _MAX_POLICY_BYTES:
                    return SubagentPolicyConfig()
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return SubagentPolicyConfig()
        if not isinstance(value, dict):
            return SubagentPolicyConfig()
        mode = str(value.get("mode") or POLICY_NATIVE)
        if mode not in SUPPORTED_POLICIES:
            mode = POLICY_NATIVE
        model = str(value.get("default_model") or "gemini-3.7-flash")
        effort = str(value.get("default_reasoning_effort") or "high").lower()
        if effort not in {"low", "medium", "high"}:
            effort = "high"
        return SubagentPolicyConfig(mode, model, effort)

    def write(self, config: SubagentPolicyConfig) -> None:
        if config.mode not in SUPPORTED_POLICIES:
            raise ValueError("Unsupported subagent policy")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        data = json.dumps(asdict(config), ensure_ascii=False, indent=2).encode("utf-8")
        with self._lock:
            try:
                temporary.write_bytes(data)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def apply(self, payload: dict[str, Any], headers: Iterable[Header]) -> AppliedSubagentPolicy:
        kind = _subagent_kind(headers)
        config = self.read()
        if config.mode != POLICY_FORCE_GEMINI or kind not in _FORCEABLE_SUBAGENTS:
            return AppliedSubagentPolicy(payload, kind, False)
        normalized = dict(payload)
        normalized["model"] = "gemini-3.7-flash"
        reasoning = normalized.get("reasoning")
        normalized_reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
        normalized_reasoning["effort"] = "high"
        normalized["reasoning"] = normalized_reasoning
        return AppliedSubagentPolicy(normalized, kind, True)


def _subagent_kind(headers: Iterable[Header]) -> str | None:
    for header in headers:
        if header.name.casefold() == "x-openai-subagent":
            value = header.value.strip().casefold()
            return value or None
    return None
