"""Bounded in-memory cache for read-only Codex product metadata.

Only explicitly allowlisted GET endpoints are cached.  Model responses, MCP
traffic, mutations and credentials never enter this cache.  Every entry is
partitioned by ChatGPT account and authorization token and disappears with the
Native Host process.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .contracts import Header, ResolvedRoute


_GLOBAL_PLUGIN_LIST_PATH = "/backend-api/ps/plugins/list"
_DEFAULT_MAX_ENTRIES = 256
_DEFAULT_MAX_ENTRY_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_CACHEABLE_GET_POLICIES = {
    _GLOBAL_PLUGIN_LIST_PATH: (10 * 60, 8 * 1024 * 1024, True),
    "/backend-api/ps/plugins/installed": (30, 4 * 1024 * 1024, True),
    "/backend-api/ps/plugins/suggested": (5 * 60, 4 * 1024 * 1024, True),
    "/backend-api/plugins/featured": (5 * 60, 4 * 1024 * 1024, False),
    "/backend-api/connectors/directory/list": (10 * 60, 8 * 1024 * 1024, False),
    "/backend-api/wham/accounts/check": (2 * 60, 1024 * 1024, False),
}


@dataclass(frozen=True, slots=True)
class CachePolicy:
    key: str
    ttl_seconds: float
    max_body_bytes: int


@dataclass(frozen=True, slots=True)
class CachedResponse:
    status: int
    headers: tuple[Header, ...]
    body: bytes
    stored_at: float


@dataclass(frozen=True, slots=True)
class CacheAccess:
    cached: CachedResponse | None = None
    age_ms: int | None = None
    owner: bool = False
    wait_event: threading.Event | None = None


class ProductResponseCache:
    """Thread-safe LRU cache with account partitioning and hard memory caps."""

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._entries: OrderedDict[str, CachedResponse] = OrderedDict()
        self._in_flight: dict[str, threading.Event] = {}
        self._total_bytes = 0
        self._lock = threading.Lock()

    def policy(
        self,
        method: str,
        route: ResolvedRoute,
        headers: list[Header],
    ) -> CachePolicy | None:
        """Return a policy only for authenticated, allowlisted metadata GETs."""

        if method != "GET" or route.name != "chatgpt-backend":
            return None
        upstream = urlsplit(route.upstream_url)
        if (
            upstream.scheme != "https"
            or upstream.hostname != "chatgpt.com"
            or upstream.port not in {None, 443}
        ):
            return None
        endpoint_policy = _CACHEABLE_GET_POLICIES.get(upstream.path)
        if endpoint_policy is None:
            return None
        ttl_seconds, max_body_bytes, require_global_scope = endpoint_policy
        query = parse_qs(upstream.query, keep_blank_values=True)
        if require_global_scope and query.get("scope") != ["GLOBAL"]:
            return None
        partition = _account_partition(headers)
        if partition is None:
            return None
        digest = hashlib.sha256(
            f"{partition}\0{route.upstream_url}\0{_header_partition(headers)}".encode("utf-8")
        ).hexdigest()
        return CachePolicy(
            key=digest,
            ttl_seconds=ttl_seconds,
            max_body_bytes=min(max_body_bytes, _DEFAULT_MAX_ENTRY_BYTES),
        )

    def get(self, policy: CachePolicy) -> tuple[CachedResponse, int] | None:
        now = time.monotonic()
        with self._lock:
            return self._get_locked(policy, now)

    def acquire(self, policy: CachePolicy) -> CacheAccess:
        """Return a hit, reserve the upstream fetch, or join the existing fetch."""

        now = time.monotonic()
        with self._lock:
            cached = self._get_locked(policy, now)
            if cached is not None:
                entry, age_ms = cached
                return CacheAccess(cached=entry, age_ms=age_ms)
            wait_event = self._in_flight.get(policy.key)
            if wait_event is not None:
                return CacheAccess(wait_event=wait_event)
            wait_event = threading.Event()
            self._in_flight[policy.key] = wait_event
            return CacheAccess(owner=True, wait_event=wait_event)

    def complete(self, policy: CachePolicy) -> None:
        """Release waiters after either a successful fill or a failed fetch."""

        with self._lock:
            wait_event = self._in_flight.pop(policy.key, None)
        if wait_event is not None:
            wait_event.set()

    def put(
        self,
        policy: CachePolicy,
        *,
        status: int,
        headers: tuple[Header, ...],
        body: bytes,
    ) -> bool:
        if status != 200 or len(body) > policy.max_body_bytes:
            return False
        response_headers: dict[str, list[str]] = {}
        for header in headers:
            response_headers.setdefault(header.name.lower(), []).append(header.value)
        if "set-cookie" in response_headers:
            return False
        cache_control = ",".join(response_headers.get("cache-control", ())).lower()
        directives = {item.strip().split("=", 1)[0] for item in cache_control.split(",")}
        if directives & {"no-store", "no-cache"}:
            return False
        pragma = ",".join(response_headers.get("pragma", ())).lower()
        if "no-cache" in {item.strip() for item in pragma.split(",")}:
            return False
        vary = ",".join(response_headers.get("vary", ()))
        if "*" in {item.strip() for item in vary.split(",")}:
            return False
        content_types = response_headers.get("content-type", ())
        if not content_types or not _is_json_content_type(content_types[-1]):
            return False
        entry = CachedResponse(
            status=status,
            headers=headers,
            body=body,
            stored_at=time.monotonic(),
        )
        with self._lock:
            previous = self._entries.pop(policy.key, None)
            if previous is not None:
                self._total_bytes -= len(previous.body)
            self._entries[policy.key] = entry
            self._total_bytes += len(body)
            while (
                len(self._entries) > self._max_entries
                or self._total_bytes > self._max_total_bytes
            ):
                _key, evicted = self._entries.popitem(last=False)
                self._total_bytes -= len(evicted.body)
        return True

    def _delete_locked(self, key: str, entry: CachedResponse) -> None:
        self._entries.pop(key, None)
        self._total_bytes -= len(entry.body)

    def _get_locked(
        self,
        policy: CachePolicy,
        now: float,
    ) -> tuple[CachedResponse, int] | None:
        entry = self._entries.get(policy.key)
        if entry is None:
            return None
        age = now - entry.stored_at
        if age > policy.ttl_seconds:
            self._delete_locked(policy.key, entry)
            return None
        self._entries.move_to_end(policy.key)
        return entry, max(0, round(age * 1000))


def _account_partition(headers: list[Header]) -> str | None:
    authorizations = [
        header.value.strip()
        for header in headers
        if header.name.lower() == "authorization" and header.value.strip()
    ]
    if not authorizations:
        return None
    token_digest = hashlib.sha256(authorizations[-1].encode("utf-8")).hexdigest()
    account_ids = [
        header.value.strip()
        for header in headers
        if header.name.lower() == "chatgpt-account-id" and header.value.strip()
    ]
    if account_ids:
        account_digest = hashlib.sha256(account_ids[-1].encode("utf-8")).hexdigest()
        return f"account:{account_digest}:authorization:{token_digest}"
    return "authorization:" + token_digest


def _header_partition(headers: list[Header]) -> str:
    """Hash all client-supplied headers so every possible Vary input is isolated."""

    canonical = "\0".join(
        f"{header.name.strip().lower()}\0{header.value.strip()}"
        for header in sorted(headers, key=lambda value: (value.name.lower(), value.value))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_json_content_type(value: str) -> bool:
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")
