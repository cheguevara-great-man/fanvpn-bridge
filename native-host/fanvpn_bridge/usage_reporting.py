"""Privacy-preserving token accounting with a durable asynchronous outbox."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import urlsplit

from .contracts import EgressRequest, Header, RequestDispatcher, ResolvedRoute


_LOG = logging.getLogger("fanvpn_bridge.usage")
_LOG.addHandler(logging.NullHandler())
_CONFIG_NAME = "usage-reporting.json"
_DATABASE_NAME = "usage-outbox.sqlite3"
_MAX_EVENT_BYTES = 16 * 1024
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    model: str = "unknown"
    model_level: str = "default"


class UsageExtractor:
    """Extract final usage from JSON or SSE without retaining response content."""

    def __init__(self) -> None:
        self._line = bytearray()
        self._whole = bytearray()
        self._tail = bytearray()
        self._best: TokenUsage | None = None

    def feed(self, data: bytes) -> None:
        if len(self._whole) < _MAX_CAPTURE_BYTES:
            self._whole.extend(data[: _MAX_CAPTURE_BYTES - len(self._whole)])
        self._tail.extend(data)
        if len(self._tail) > _MAX_CAPTURE_BYTES:
            del self._tail[: len(self._tail) - _MAX_CAPTURE_BYTES]
        self._line.extend(data)
        while True:
            newline = self._line.find(b"\n")
            if newline < 0:
                if len(self._line) > _MAX_CAPTURE_BYTES:
                    self._line.clear()
                return
            line = bytes(self._line[:newline]).strip()
            del self._line[: newline + 1]
            if line.startswith(b"data:"):
                self._inspect_json(line[5:].strip())

    def finish(self) -> TokenUsage | None:
        self._inspect_json(bytes(self._whole).strip())
        self._inspect_json(bytes(self._tail).strip())
        self._whole.clear()
        self._tail.clear()
        self._line.clear()
        return self._best

    def _inspect_json(self, raw: bytes) -> None:
        if not raw or raw == b"[DONE]" or b'"usage"' not in raw:
            return
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            usage = _find_usage_bytes(raw)
            if usage is not None and (self._best is None or usage.total_tokens >= self._best.total_tokens):
                self._best = usage
            return
        usage = _find_usage(value)
        if usage is not None and (self._best is None or usage.total_tokens >= self._best.total_tokens):
            self._best = usage


class _ReportSink:
    def __init__(self) -> None:
        self.status: int | None = None
        self.done = threading.Event()
        self.error: Exception | None = None

    def start(self, status: int, headers: Sequence[Header], timing=None) -> None:
        self.status = status

    def write(self, data: bytes, on_consumed: Callable[[], None] | None = None) -> None:
        if on_consumed is not None:
            on_consumed()

    def finish(self) -> None:
        self.done.set()

    def fail(self, error: Exception) -> None:
        self.error = error
        self.done.set()


class UsageReporter:
    """Persist usage first, then deliver it through the existing browser channel."""

    def __init__(
        self,
        runtime_directory: Path,
        dispatcher: RequestDispatcher,
        *,
        collector_url: str,
        report_token: str,
        machine_id: str,
        machine_name: str,
    ) -> None:
        self._dispatcher = dispatcher
        self._collector_url = collector_url.rstrip("/")
        self._report_token = report_token
        self._machine_id = machine_id
        self._machine_name = machine_name
        self._database_path = runtime_directory / _DATABASE_NAME
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._database_lock = threading.Lock()
        self._initialize_database()
        self._thread = threading.Thread(target=self._run, name="fanvpn-usage-reporter", daemon=True)
        self._thread.start()

    @classmethod
    def load(cls, runtime_directory: Path, dispatcher: RequestDispatcher) -> UsageReporter | None:
        path = runtime_directory / _CONFIG_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            collector_url = _required_string(raw, "collector_url")
            report_token = _required_string(raw, "report_token")
            machine_id = _required_string(raw, "machine_id")
            machine_name = _required_string(raw, "machine_name")
            _validate_collector_url(collector_url)
            uuid.UUID(machine_id)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            _LOG.warning("usage_config_invalid error=%s", type(error).__name__)
            return None
        runtime_directory.mkdir(parents=True, exist_ok=True)
        return cls(
            runtime_directory,
            dispatcher,
            collector_url=collector_url,
            report_token=report_token,
            machine_id=machine_id,
            machine_name=machine_name[:128],
        )

    def record(self, usage: TokenUsage, *, route: str) -> None:
        event = {
            "event_id": str(uuid.uuid4()),
            "machine_id": self._machine_id,
            "machine_name": self._machine_name,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "route": route[:64],
            **asdict(usage),
        }
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > _MAX_EVENT_BYTES:
            return
        with self._database_lock, closing(self._connect()) as database, database:
            database.execute(
                "INSERT OR IGNORE INTO usage_outbox(event_id, payload, created_at) VALUES (?, ?, ?)",
                (event["event_id"], payload, int(time.time())),
            )
        self._wake.set()

    def snapshot(self) -> dict[str, object]:
        with self._database_lock, closing(self._connect()) as database, database:
            pending = database.execute("SELECT COUNT(*) FROM usage_outbox").fetchone()[0]
            delivered = database.execute("SELECT COUNT(*) FROM usage_delivered").fetchone()[0]
            totals = database.execute(
                "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(input_tokens), 0), "
                "COALESCE(SUM(output_tokens), 0) FROM usage_delivered"
            ).fetchone()
        return {
            "enabled": True,
            "machine_id": self._machine_id,
            "machine_name": self._machine_name,
            "pending_events": pending,
            "delivered_events": delivered,
            "delivered_total_tokens": totals[0],
            "delivered_input_tokens": totals[1],
            "delivered_output_tokens": totals[2],
        }

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        delay = 1.0
        while not self._closed.is_set():
            row = self._next_event()
            if row is None:
                self._wake.wait(30)
                self._wake.clear()
                continue
            event_id, payload = row
            if self._deliver(event_id, payload):
                delay = 1.0
                continue
            self._wake.wait(delay)
            self._wake.clear()
            delay = min(delay * 2, 300.0)

    def _deliver(self, event_id: str, payload: str) -> bool:
        parts = urlsplit(self._collector_url)
        route = ResolvedRoute(
            name="usage-collector",
            upstream_base_url=f"{parts.scheme}://{parts.netloc}",
            upstream_url=self._collector_url,
        )
        request_id = "usage_" + uuid.uuid4().hex
        sink = _ReportSink()
        body = payload.encode("utf-8")
        try:
            self._dispatcher.submit(
                EgressRequest(
                    request_id=request_id,
                    method="POST",
                    route=route,
                    headers=(
                        Header("accept", "application/json"),
                        Header("authorization", f"Bearer {self._report_token}"),
                        Header("content-type", "application/json"),
                    ),
                ),
                (body,),
                sink,
            )
            if not sink.done.wait(30) or sink.error is not None or sink.status not in {200, 201, 202}:
                return False
            parsed = json.loads(payload)
            with self._database_lock, closing(self._connect()) as database, database:
                database.execute(
                    "INSERT OR IGNORE INTO usage_delivered(event_id, input_tokens, output_tokens, total_tokens, delivered_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        event_id,
                        parsed["input_tokens"],
                        parsed["output_tokens"],
                        parsed["total_tokens"],
                        int(time.time()),
                    ),
                )
                database.execute("DELETE FROM usage_outbox WHERE event_id = ?", (event_id,))
                database.execute(
                    "DELETE FROM usage_delivered WHERE delivered_at < ?",
                    (int(time.time()) - 90 * 86400,),
                )
            return True
        except Exception as error:
            _LOG.info("usage_delivery_deferred error=%s", type(error).__name__)
            return False

    def _next_event(self) -> tuple[str, str] | None:
        with self._database_lock, closing(self._connect()) as database, database:
            row = database.execute(
                "SELECT event_id, payload FROM usage_outbox ORDER BY created_at, event_id LIMIT 1"
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def _initialize_database(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._database_lock, closing(self._connect()) as database, database:
            database.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS usage_outbox(
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_delivered(
                    event_id TEXT PRIMARY KEY, input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    delivered_at INTEGER NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path, timeout=5)


def _find_usage(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get("usage")
    response = value.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        candidate = response["usage"]
        model = response.get("model", value.get("model", "unknown"))
        reasoning = response.get("reasoning")
        model_level = (
            reasoning.get("effort")
            if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str)
            else response.get("service_tier", "default")
        )
    else:
        model = value.get("model", "unknown")
        reasoning = value.get("reasoning")
        model_level = (
            reasoning.get("effort")
            if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str)
            else value.get("service_tier", "default")
        )
    if not isinstance(candidate, dict):
        return None
    input_tokens = _token_int(candidate, "input_tokens", "prompt_tokens")
    output_tokens = _token_int(candidate, "output_tokens", "completion_tokens")
    total_tokens = _token_int(candidate, "total_tokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    if total_tokens <= 0 or input_tokens < 0 or output_tokens < 0:
        return None
    input_details = candidate.get("input_tokens_details")
    output_details = candidate.get("output_tokens_details")
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=_detail_int(input_details, "cached_tokens"),
        reasoning_output_tokens=_detail_int(output_details, "reasoning_tokens"),
        model=str(model)[:128] if isinstance(model, str) and model else "unknown",
        model_level=(
            str(model_level)[:64]
            if isinstance(model_level, str) and model_level
            else "default"
        ),
    )


def _find_usage_bytes(raw: bytes) -> TokenUsage | None:
    """Recover the final usage object from a very large SSE completion event."""

    matches = list(re.finditer(rb'"usage"\s*:\s*\{', raw))
    if not matches:
        return None
    fragment = raw[matches[-1].start() :]

    def number(*names: bytes) -> int:
        for name in names:
            match = re.search(rb'"' + re.escape(name) + rb'"\s*:\s*(\d+)', fragment)
            if match:
                return int(match.group(1))
        return 0

    input_tokens = number(b"input_tokens", b"prompt_tokens")
    output_tokens = number(b"output_tokens", b"completion_tokens")
    total_tokens = number(b"total_tokens") or input_tokens + output_tokens
    if total_tokens <= 0:
        return None
    model_matches = list(re.finditer(rb'"model"\s*:\s*"([^"\\]{1,128})"', raw))
    model = model_matches[-1].group(1).decode("utf-8", "replace") if model_matches else "unknown"
    effort_matches = list(re.finditer(rb'"effort"\s*:\s*"([^"\\]{1,64})"', raw))
    model_level = (
        effort_matches[-1].group(1).decode("utf-8", "replace")
        if effort_matches
        else "default"
    )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=number(b"cached_tokens"),
        reasoning_output_tokens=number(b"reasoning_tokens"),
        model=model,
        model_level=model_level,
    )


def _token_int(values: dict[str, object], *names: str) -> int:
    for name in names:
        value = values.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _detail_int(value: object, name: str) -> int:
    return _token_int(value, name) if isinstance(value, dict) else 0


def _required_string(values: object, name: str) -> str:
    if not isinstance(values, dict) or not isinstance(values.get(name), str) or not values[name]:
        raise ValueError(f"{name} is required")
    return str(values[name])


def _validate_collector_url(value: str) -> None:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path != "/v1/usage/events"
    ):
        raise ValueError("collector_url must be an HTTPS /v1/usage/events URL")
