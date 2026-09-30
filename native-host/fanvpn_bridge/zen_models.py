"""Discovery and reachability probing for OpenCode Zen free models.

Zen publishes a curated catalog through models.dev and serves every entry from a
single OpenAI-compatible endpoint. A catalog entry marked ``cost: 0`` is not
necessarily reachable without credentials: the upstream reserves anonymous
access for a subset of those models. This module narrows the catalog to models
that actually answer an unauthenticated request and keeps that verified set in a
cache so a later refresh can retire entries that stopped working.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ZEN_API_BASE = "https://opencode.ai/zen/v1"
MODELS_DEV_URL = "https://models.dev/api.json"
ZEN_PROVIDER_ID = "opencode"
#: Slug prefix used for every catalog entry this module produces. It keeps Zen
#: rows disjoint from the ``gemini-*``, ``deepseek-web/*`` and ``chatgpt-web/*``
#: namespaces so no slug or display name can collide with an existing provider.
ZEN_SLUG_PREFIX = "zen/"
#: Display-name marker. Codex's model picker is a flat list, so the prefix is
#: what makes Zen rows visually identifiable as a family.
ZEN_DISPLAY_PREFIX = "Zen"
_PROBE_PROMPT = "ping"
_PROBE_MAX_TOKENS = 8
_PROBE_WORKERS = 6
_PROBE_TIMEOUT_SECONDS = 30.0
_CATALOG_TIMEOUT_SECONDS = 60.0
_MAX_PROBE_BODY = 4 * 1024 * 1024
#: models.dev serves every provider in one document, currently about 5 MiB, so
#: the catalog fetch needs a larger ceiling than the probe responses.
_MAX_CATALOG_BODY = 32 * 1024 * 1024
#: A model must fail this many consecutive refreshes before it is dropped from
#: the catalog. One transient failure should not make a working model vanish
#: from the picker.
FAILURE_RETIREMENT_THRESHOLD = 3
#: Cloudflare rejects the stock ``python-urllib`` agent with error 1010, so
#: every request identifies the Bridge explicitly.
_USER_AGENT = "FanVPNBridge/1.0 (+opencode-zen-catalog)"
#: Verified metadata older than this is re-probed so capability fields such as
#: ``context_window`` track upstream changes.
_CATALOG_MAX_AGE_SECONDS = 24 * 60 * 60
_DEFAULT_CONTEXT_WINDOW = 1_000_000
_EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")


class ZenModelError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        code: str = "zen_model_error",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _fetch_json(url: str, *, timeout: float, max_bytes: int = _MAX_CATALOG_BODY) -> Any:
    request = urllib.request.Request(
        url,
        headers={"accept": "application/json", "user-agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ZenModelError(f"Zen catalog request failed: {exc}") from exc
    if len(raw) > max_bytes:
        raise ZenModelError("Zen catalog response exceeded the bridge safety limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ZenModelError("Zen catalog returned invalid JSON") from exc


def _free_model_ids_from(document: object) -> list[str]:
    if not isinstance(document, Mapping):
        raise ZenModelError("models.dev returned an unexpected document")
    provider = document.get(ZEN_PROVIDER_ID)
    if not isinstance(provider, Mapping):
        raise ZenModelError("models.dev document has no Zen provider entry")
    models = provider.get("models")
    if not isinstance(models, Mapping):
        raise ZenModelError("models.dev Zen provider has no model map")

    model_ids: list[str] = []
    for model_id, metadata in models.items():
        if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
            continue
        cost = metadata.get("cost")
        if not isinstance(cost, Mapping):
            continue
        if cost.get("input") == 0 and cost.get("output") == 0:
            model_ids.append(model_id)
    return sorted(model_ids)


def catalog_free_model_ids() -> list[str]:
    """Return every models.dev Zen entry advertised at zero cost."""

    return _free_model_ids_from(_fetch_json(MODELS_DEV_URL, timeout=_CATALOG_TIMEOUT_SECONDS))


def live_model_ids() -> set[str]:
    """Return the model ids the Zen endpoint currently serves.

    Anonymous access is required here, so a 401 response is a real failure
    rather than a missing credential.
    """

    document = _fetch_json(f"{ZEN_API_BASE}/models", timeout=_CATALOG_TIMEOUT_SECONDS)
    if not isinstance(document, Mapping) or not isinstance(document.get("data"), list):
        raise ZenModelError("Zen model list has an unexpected shape")
    live: set[str] = set()
    for entry in document["data"]:
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
            live.add(entry["id"])
    return live


def probe_model(model_id: str, *, timeout: float = _PROBE_TIMEOUT_SECONDS) -> bool:
    """Return whether the model answers an unauthenticated completion."""

    body = json.dumps(
        {
            "model": model_id,
            "messages": [{"role": "user", "content": _PROBE_PROMPT}],
            "max_tokens": _PROBE_MAX_TOKENS,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{ZEN_API_BASE}/chat/completions",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_PROBE_BODY + 1)
    except urllib.error.HTTPError:
        # 400/403 are the documented shapes for a model that exists but is not
        # anonymously reachable. Any HTTP status is a failed probe.
        return False
    except (OSError, urllib.error.URLError):
        return False
    if len(raw) > _MAX_PROBE_BODY:
        return False
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, Mapping):
        return False
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    return isinstance(message, Mapping) and isinstance(message.get("content"), (str, type(None)))


def _probe_all(model_ids: list[str]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    if not model_ids:
        return results
    lock = threading.Lock()
    index = threading.Lock()
    cursor = [0]

    def worker() -> None:
        while True:
            with index:
                position = cursor[0]
                cursor[0] += 1
            if position >= len(model_ids):
                return
            model_id = model_ids[position]
            reachable = probe_model(model_id)
            with lock:
                results[model_id] = reachable

    workers = [
        threading.Thread(target=worker, name="zen-probe", daemon=True)
        for _ in range(min(_PROBE_WORKERS, len(model_ids)))
    ]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    return results


def _display_name(model_id: str, metadata: Mapping[str, Any]) -> str:
    name = metadata.get("name")
    if isinstance(name, str) and name.strip():
        return f"{ZEN_DISPLAY_PREFIX} \u203a {name.strip()}"
    return f"{ZEN_DISPLAY_PREFIX} \u203a {model_id}"


def _context_window(metadata: Mapping[str, Any]) -> int:
    limit = metadata.get("limit")
    if isinstance(limit, Mapping):
        context = limit.get("context")
        if isinstance(context, int) and context > 0:
            return context
    return _DEFAULT_CONTEXT_WINDOW


def _supports_reasoning(metadata: Mapping[str, Any]) -> bool:
    return bool(metadata.get("reasoning"))


def _supported_efforts(metadata: Mapping[str, Any]) -> list[str]:
    if not _supports_reasoning(metadata):
        return []
    options = metadata.get("reasoning_options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, Mapping):
                continue
            values = option.get("values")
            if isinstance(values, list):
                known = [value for value in values if isinstance(value, str) and value in _EFFORT_LADDER]
                if known:
                    return known
    # The Zen endpoint reasons by default, so a reasoning model without an
    # explicit ladder still offers the standard Codex efforts.
    return ["low", "medium", "high"]


def build_catalog_entry(model_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Build one Responses-shaped catalog row for a verified free model."""

    context_window = _context_window(metadata)
    efforts = _supported_efforts(metadata)
    modalities = metadata.get("modalities")
    input_modalities = ["text"]
    if isinstance(modalities, Mapping):
        model_input = modalities.get("input")
        if isinstance(model_input, list) and "image" in model_input:
            input_modalities.append("image")
    entry: dict[str, Any] = {
        "id": ZEN_SLUG_PREFIX + model_id,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "opencode-zen",
        "display_name": _display_name(model_id, metadata),
        "description": (
            "Free OpenCode Zen model reachable without credentials. "
            "Availability is verified by probing the live endpoint."
        ),
        "context_window": context_window,
        "max_context_window": context_window,
        "effective_context_window_percent": 90,
        "auto_compact_token_limit": int(context_window * 0.9),
        "input_modalities": input_modalities,
    }
    if efforts:
        entry["default_reasoning_level"] = "medium" if "medium" in efforts else efforts[0]
        entry["supported_reasoning_levels"] = efforts
    return entry


def _zen_model_metadata(document: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(document, Mapping):
        return {}
    provider = document.get(ZEN_PROVIDER_ID)
    if not isinstance(provider, Mapping):
        return {}
    models = provider.get("models")
    if not isinstance(models, Mapping):
        return {}
    return {
        key: value
        for key, value in models.items()
        if isinstance(key, str) and isinstance(value, Mapping)
    }


class ZenModelCatalog:
    """Verified, persistent view of the anonymously reachable Zen models."""

    def __init__(self, *, cache_path: Path | None = None) -> None:
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if self._cache_path is None or not self._cache_path.is_file():
            return
        try:
            document = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(document, Mapping):
            return
        models = document.get("models")
        if not isinstance(models, Mapping):
            return
        for slug, record in models.items():
            if not isinstance(slug, str) or not isinstance(record, Mapping):
                continue
            entry = record.get("entry")
            if isinstance(entry, Mapping):
                self._entries[slug] = dict(entry)
            failures = record.get("consecutive_failures")
            if isinstance(failures, int) and failures > 0:
                self._failures[slug] = failures

    def _save(self) -> None:
        if self._cache_path is None:
            return
        models: dict[str, Any] = {}
        for slug, entry in self._entries.items():
            models[slug] = {
                "entry": entry,
                "verified_at": int(time.time()),
                "consecutive_failures": self._failures.get(slug, 0),
            }
        document = {
            "version": 1,
            "api_base": ZEN_API_BASE,
            "failure_retirement_threshold": FAILURE_RETIREMENT_THRESHOLD,
            "models": models,
        }
        temporary = self._cache_path.with_name(self._cache_path.name + f".tmp{threading.get_ident()}")
        try:
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self._cache_path)
        except OSError:
            # A cache miss degrades to re-probing on the next refresh, which is
            # strictly better than failing a turn that already has a catalog.
            return
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._entries.values()]

    def resolve_model_id(self, value: object) -> str | None:
        """Map a Codex ``zen/<upstream-id>`` slug to the bare upstream id."""

        if not isinstance(value, str) or not value.startswith(ZEN_SLUG_PREFIX):
            return None
        model_id = value[len(ZEN_SLUG_PREFIX):].strip()
        if not model_id:
            return None
        with self._lock:
            entry = self._entries.get(value)
        if entry is not None:
            return model_id
        # An unverified slug is still forwarded. The upstream rejects it with a
        # real error, which is more useful than a bridge-level "unknown model".
        return model_id

    def refresh(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Re-discover and re-probe the free Zen models.

        A failed probe increments a per-model counter and only retires the
        entry after :data:`FAILURE_RETIREMENT_THRESHOLD` consecutive misses, so
        a single flaky endpoint cannot empty the picker.
        """

        if not force and not self._is_stale():
            return self.entries()

        document = _fetch_json(MODELS_DEV_URL, timeout=_CATALOG_TIMEOUT_SECONDS)
        free_ids = _free_model_ids_from(document)
        live = live_model_ids()
        candidates = [model_id for model_id in free_ids if model_id in live]
        metadata = _zen_model_metadata(document)
        reachability = _probe_all(candidates)

        with self._lock:
            verified: dict[str, dict[str, Any]] = {}
            failures: dict[str, int] = {}
            for model_id, reachable in reachability.items():
                slug = ZEN_SLUG_PREFIX + model_id
                if reachable:
                    verified[slug] = build_catalog_entry(model_id, metadata.get(model_id, {}))
                    continue
                previous = self._entries.get(slug)
                if previous is None:
                    continue
                count = self._failures.get(slug, 0) + 1
                if count < FAILURE_RETIREMENT_THRESHOLD:
                    # Keep the last known good row so one flaky probe cannot
                    # empty the picker.
                    verified[slug] = previous
                    failures[slug] = count
            # A model that stopped appearing in the live list is not a probe
            # failure, so its counter is dropped along with the row.
            self._entries = verified
            self._failures = failures
            self._save()
            return [dict(entry) for entry in self._entries.values()]

    def _is_stale(self) -> bool:
        if not self._entries:
            return True
        if self._cache_path is None or not self._cache_path.is_file():
            return True
        try:
            age = time.time() - self._cache_path.stat().st_mtime
        except OSError:
            return True
        return age > _CATALOG_MAX_AGE_SECONDS


__all__ = [
    "FAILURE_RETIREMENT_THRESHOLD",
    "ZEN_API_BASE",
    "ZEN_DISPLAY_PREFIX",
    "ZEN_SLUG_PREFIX",
    "ZenModelCatalog",
    "ZenModelError",
    "build_catalog_entry",
    "catalog_free_model_ids",
    "live_model_ids",
    "probe_model",
]
